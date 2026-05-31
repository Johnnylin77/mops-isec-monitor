#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MOPS 資安重訊監控系統

使用公開資訊觀測站「公告快易查」(ezsearch) 全文檢索，
以關鍵字比對重大訊息的「主旨 + 內文」，找出資安相關公告。
純 HTTP 請求，不需瀏覽器。
"""

import sys
import io
import os
import re
import time
import json
import smtplib
from urllib.parse import quote, urlparse, parse_qs
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess

import requests
from bs4 import BeautifulSoup

if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# ─────────────────────────── 設定 ───────────────────────────
# 寄件者與收件人皆由環境變數/GitHub Secret 提供，避免 email 出現在公開原始碼。
#   EMAIL_SENDER     : 寄件 Gmail（專用帳號）
#   EMAIL_RECEIVERS  : 收件人，多人以逗號分隔，例如 a@x.com, b@y.com
#   GMAIL_APP_PASSWORD: 寄件帳號的應用程式密碼
EMAIL_SENDER = os.environ.get("EMAIL_SENDER", "").strip()
EMAIL_RECEIVERS = [e.strip() for e in os.environ.get("EMAIL_RECEIVERS", "").split(",") if e.strip()]
if not EMAIL_RECEIVERS and EMAIL_SENDER:
    EMAIL_RECEIVERS = [EMAIL_SENDER]  # 未設定收件人時，預設寄給自己

EZSEARCH_URL = "https://mopsov.twse.com.tw/mops/web/ezsearch_query"
AUDITOR_URL = "https://mopsov.twse.com.tw/mops/web/ajax_t05st03"
DETAIL_URL = "https://mopsov.twse.com.tw/mops/web/ajax_t05sr01_1"

# 產生的分析簡報檔名
PPTX_FILE = Path("資安事件因應分析.pptx")

# 記錄「前一次報告」的公告清單（用於和這次比對，標記新出現的資安事件 NEW）
PREV_FILE = Path("reports") / "previous_announcements.json"

# 資安事件相關關鍵字（比對主旨 + 內文）
# 註：原「系統異常」太籠統會誤中公開收購樣板，改用更精準的「資訊系統異常／資通系統異常」
KEYWORDS = [
    "資安事件",
    "資安緊急應變",
    "資訊系統異常",
    "資通系統異常",
    "駭客攻擊",
    "攻擊事件",
    "網路攻擊",
    "駭客網路攻擊",
    "入侵",
    "異常通報",
    "加密攻擊",
]

# 市場別：sii=上市, otc=上櫃, rotc=興櫃, pub=公開發行
MARKETS = ["sii", "otc", "rotc", "pub"]


# 台灣時區（UTC+8），確保本機與雲端（CI 為 UTC）的日期一致
TW_TZ = timezone(timedelta(hours=8))


def now_tw():
    """目前的台灣時間（UTC+8）"""
    return datetime.now(TW_TZ)


def roc_date(dt):
    """西元 datetime → 民國年字串 115/05/30"""
    return f"{dt.year - 1911}/{dt.month:02d}/{dt.day:02d}"


def ezsearch(keyword, market, sdate, edate, session):
    """對單一關鍵字 + 市場，呼叫 ezsearch 全文檢索，回傳公告列表"""
    subject = quote(keyword)  # 等同前端 encodeURIComponent
    # 注意：日期需保留斜線（不可 URL-encode），AN 須為空、TYPEK 為市場別
    body = (
        f"step=00&RADIO_CM=1&TYPEK={market}&CO_MARKET=&CO_ID=&PRO_ITEM="
        f"&SUBJECT={subject}&SDATE={sdate}&EDATE={edate}&lang=TW&AN="
    )
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": "https://mopsov.twse.com.tw/mops/web/ezsearch",
        "User-Agent": "Mozilla/5.0",
    }
    resp = session.post(EZSEARCH_URL, data=body.encode("utf-8"), headers=headers, timeout=30)
    text = resp.content.decode("utf-8", errors="replace")
    brace = text.find("{")
    if brace < 0:
        return []
    try:
        data = json.loads(text[brace:])
    except json.JSONDecodeError:
        return []
    return data.get("data", []) or []


def get_auditor(co_id, session):
    """查詢公司目前的簽證會計師事務所與會計師名稱

    回傳 (事務所名稱, [會計師1, 會計師2])
    """
    data = {
        "encodeURIComponent": "1", "step": "1", "firstin": "1", "off": "1",
        "keyword4": "", "code1": "", "TYPEK": "all", "co_id": co_id,
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": "https://mopsov.twse.com.tw/mops/web/t05st03",
        "User-Agent": "Mozilla/5.0",
    }
    try:
        resp = session.post(AUDITOR_URL, data=data, headers=headers, timeout=30)
        soup = BeautifulSoup(resp.content.decode("utf-8", errors="replace"), "html.parser")
        firm, cpas = "", []
        for cell in soup.find_all(["th", "td"]):
            label = cell.get_text(strip=True)
            if label == "簽證會計師事務所":
                nxt = cell.find_next_sibling()
                if nxt:
                    firm = nxt.get_text(strip=True)
            elif label in ("簽證會計師1", "簽證會計師2"):
                nxt = cell.find_next_sibling()
                if nxt and nxt.get_text(strip=True):
                    cpas.append(nxt.get_text(strip=True))
        return firm, cpas
    except Exception as e:
        print(f"  [!] 查詢 {co_id} 會計師失敗: {e}")
        return "", []


def classify_event_type(text):
    """依關鍵字將事件分類"""
    if any(k in text for k in ("勒索", "加密")):
        return "勒索病毒/加密"
    if any(k in text for k in ("個資", "個人資料", "外洩", "外流", "消費者")):
        return "個資外洩"
    if any(k in text for k in ("駭客", "網路攻擊", "入侵", "攻擊")):
        return "駭客攻擊"
    return "其他"


def get_incident_detail(link, session):
    """依公告 HYPERLINK 抓取完整內文，解析發生緣由 / 影響 / 因應措施"""
    result = {"cause": "", "impact": "", "future": "", "raw": ""}
    if not link:
        return result
    qs = parse_qs(urlparse(link).query)
    params = {
        "firstin": "true", "stp": "1", "step": "1",
        "SEQ_NO": qs.get("SEQ_NO", [""])[0],
        "SPOKE_TIME": qs.get("SPOKE_TIME", [""])[0],
        "SPOKE_DATE": qs.get("SPOKE_DATE", [""])[0],
        "COMPANY_ID": qs.get("COMPANY_ID", [""])[0],
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": "https://mopsov.twse.com.tw/mops/web/ezsearch",
        "User-Agent": "Mozilla/5.0",
    }
    try:
        resp = session.post(DETAIL_URL, data=params, headers=headers, timeout=30)
        text = BeautifulSoup(resp.content.decode("utf-8", errors="replace"),
                             "html.parser").get_text("\n", strip=True)
        result["raw"] = text
        block = text.split("說明", 1)[1] if "說明" in text else text
        block = block.split("以上資料均由", 1)[0]
        fields = {}
        for item in re.split(r"\n?\s*\d+\.", block):
            m = re.match(r"([^：:]{2,22})[：:](.*)", item.strip(), re.S)
            if m:
                fields[m.group(1).strip()] = re.sub(r"\s+", "", m.group(2))

        def pick(*kws):
            for k, v in fields.items():
                if any(kw in k for kw in kws):
                    return v
            return ""

        result["cause"] = pick("發生緣由", "緣由")
        result["impact"] = pick("損失或影響", "影響")
        result["future"] = pick("因應措施", "改善情形")
    except Exception as e:
        print(f"  [!] 抓取內文失敗: {e}")
    return result


def announcement_key(ann):
    """單一資安事件的唯一鍵：公司代號 + 發布日期 + 主旨"""
    return f"{ann['code']}-{ann['date']}-{ann['title']}"


def load_previous_keys():
    """讀取『前一次報告』的公告鍵集合"""
    if PREV_FILE.exists():
        try:
            return set(json.loads(PREV_FILE.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def save_previous_keys(keys):
    """把『這次報告』的公告鍵存起來，供下次比對"""
    PREV_FILE.parent.mkdir(exist_ok=True)
    PREV_FILE.write_text(
        json.dumps(sorted(keys), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def scrape_mops_announcements(days=30):
    """爬取過去 N 天內、符合資安關鍵字的重大訊息"""
    today = now_tw()
    sdate = roc_date(today - timedelta(days=days))
    edate = roc_date(today)

    session = requests.Session()
    announcements = []
    seen = set()

    for keyword in KEYWORDS:
        print(f"\n[*] 搜尋關鍵字: {keyword}")
        for market in MARKETS:
            try:
                items = ezsearch(keyword, market, sdate, edate, session)
            except Exception as e:
                print(f"  [!] {keyword}/{market} 查詢失敗: {e}")
                continue

            for d in items:
                code = (d.get("COMPANY_ID") or "").strip()
                company = (d.get("COMPANY_NAME") or "").strip()
                date_str = (d.get("CDATE") or "").strip()
                title = (d.get("SUBJECT") or "").strip()
                link = (d.get("HYPERLINK") or "").strip()

                if not code or not title:
                    continue

                key = f"{code}-{date_str}-{title}"
                if key in seen:
                    continue
                seen.add(key)

                announcements.append({
                    "code": code,
                    "company": company,
                    "date": date_str,
                    "title": title,
                    "keyword": keyword,
                    "link": link,
                })
                print(f"  [✓] {company} ({code}) - {title[:50]}")

            time.sleep(0.2)  # 禮貌性延遲，避免對伺服器造成負擔

    # 依日期新到舊排序
    announcements.sort(key=lambda a: a["date"], reverse=True)

    # 標記 NEW（和「前一次報告」比對，找出新出現的資安事件）+ 補上簽證會計師資訊
    previous_keys = load_previous_keys()
    auditor_cache = {}
    print(f"\n[*] 標記 NEW 並查詢簽證會計師（前一次報告共 {len(previous_keys)} 筆）...")
    for ann in announcements:
        code = ann["code"]
        ann["is_new"] = announcement_key(ann) not in previous_keys

        if code not in auditor_cache:
            firm, cpas = get_auditor(code, session)
            auditor_cache[code] = (firm, cpas)
            time.sleep(0.2)
        firm, cpas = auditor_cache[code]
        ann["auditor_firm"] = firm
        ann["auditor_cpas"] = cpas

        # 抓取內文並分類事件類型（供報告與 PPT 使用）
        detail = get_incident_detail(ann.get("link", ""), session)
        ann["cause"] = detail["cause"]
        ann["impact"] = detail["impact"]
        ann["future"] = detail["future"]
        # 以主旨 + 發生緣由分類（避免比對到內文「無個資外洩」等否定樣板而誤判）
        ann["event_type"] = classify_event_type(ann["title"] + " " + detail["cause"])
        time.sleep(0.2)

        tag = " 🆕" if ann["is_new"] else ""
        print(f"  {ann['company']} ({code}){tag} [{ann['event_type']}] - {firm} / {'、'.join(cpas)}")

    # 把「這次報告」的公告清單存起來，供下次比對
    save_previous_keys(announcement_key(a) for a in announcements)

    return announcements


def generate_report(announcements):
    today = now_tw().strftime("%Y-%m-%d")
    one_month_ago = (now_tw() - timedelta(days=30)).strftime("%Y-%m-%d")

    if not announcements:
        return f"""# MOPS 資安重訊監控報告

**報告日期：** {today}
**資料期間：** {one_month_ago} 至 {today}
**資料來源：** 台灣證券交易所公開資訊觀測站
**公告數量：** 0

---

本期間內未發現任何資安相關重大訊息公告。
"""

    markdown = f"""# MOPS 資安重訊監控報告

**報告日期：** {today}
**資料期間：** {one_month_ago} 至 {today}
**資料來源：** 台灣證券交易所公開資訊觀測站
**公告數量：** {len(announcements)}

---

"""
    for ann in announcements:
        new_tag = " `NEW`" if ann.get("is_new") else ""
        cpas = "、".join(ann.get("auditor_cpas", [])) or "—"
        firm = ann.get("auditor_firm") or "—"
        markdown += f"""## {ann['company']} ({ann['code']}){new_tag} - {ann['title']}

**發布日期：** {ann['date']}
**公司代號：** {ann['code']}
**命中關鍵字：** {ann['keyword']}
**簽證會計師事務所：** {firm}
**簽證會計師：** {cpas}
**公告連結：** {ann.get('link', '')}

---

"""
    return markdown


def generate_html_email(announcements):
    today = now_tw().strftime("%Y-%m-%d")
    one_month_ago = (now_tw() - timedelta(days=30)).strftime("%Y-%m-%d")

    if not announcements:
        return f"""
        <html><body style="font-family:Arial,'Microsoft JhengHei',sans-serif;">
        <h2 style="color:#2c3e50;">MOPS 資安重訊監控報告</h2>
        <p><b>報告日期：</b>{today}<br>
        <b>資料期間：</b>{one_month_ago} 至 {today}</p>
        <div style="padding:20px;background:#e8f5e9;border-left:4px solid #4caf50;border-radius:4px;">
            <p style="font-size:15px;color:#2e7d32;margin:0;">
                ✓ 本期間內<b>未發現</b>任何資安相關重大訊息公告。
            </p>
        </div>
        <br><p style="color:gray;font-size:12px;">資料來源：台灣證券交易所公開資訊觀測站（mopsov.twse.com.tw）</p>
        </body></html>
        """

    rows_html = ""
    for ann in announcements:
        link = ann.get("link", "")
        title_cell = (
            f'<a href="{link}" style="color:#1565c0;">{ann["title"]}</a>'
            if link else ann["title"]
        )
        new_badge = (
            '<span style="background:#e53935;color:white;font-size:11px;'
            'padding:2px 6px;border-radius:3px;margin-left:4px;">NEW</span>'
            if ann.get("is_new") else ""
        )
        firm = ann.get("auditor_firm") or "—"
        cpas = "、".join(ann.get("auditor_cpas", [])) or "—"
        rows_html += f"""
        <tr>
            <td style="padding:8px;border:1px solid #ddd;white-space:nowrap;">{ann['company']}{new_badge}</td>
            <td style="padding:8px;border:1px solid #ddd;">{ann['code']}</td>
            <td style="padding:8px;border:1px solid #ddd;white-space:nowrap;">{ann['date']}</td>
            <td style="padding:8px;border:1px solid #ddd;">{title_cell}</td>
            <td style="padding:8px;border:1px solid #ddd;">{ann['keyword']}</td>
            <td style="padding:8px;border:1px solid #ddd;white-space:nowrap;">{firm}</td>
            <td style="padding:8px;border:1px solid #ddd;white-space:nowrap;">{cpas}</td>
        </tr>"""

    return f"""
    <html><body style="font-family:Arial,'Microsoft JhengHei',sans-serif;">
    <h2 style="color:#2c3e50;">MOPS 資安重訊監控報告</h2>
    <p><b>報告日期：</b>{today}<br>
    <b>資料期間：</b>{one_month_ago} 至 {today}<br>
    <b>公告數量：</b>{len(announcements)} 筆<br>
    <span style="font-size:12px;color:#888;">標記 <b style="color:#e53935;">NEW</b> 表示首次出現的公司</span></p>
    <table style="border-collapse:collapse;width:100%;font-size:14px;">
        <tr style="background:#2c3e50;color:white;">
            <th style="padding:10px;border:1px solid #ddd;">公司名稱</th>
            <th style="padding:10px;border:1px solid #ddd;">代號</th>
            <th style="padding:10px;border:1px solid #ddd;">日期</th>
            <th style="padding:10px;border:1px solid #ddd;">主旨</th>
            <th style="padding:10px;border:1px solid #ddd;">命中關鍵字</th>
            <th style="padding:10px;border:1px solid #ddd;">簽證會計師事務所</th>
            <th style="padding:10px;border:1px solid #ddd;">簽證會計師</th>
        </tr>
        {rows_html}
    </table>
    <br><p style="color:gray;font-size:12px;">資料來源：台灣證券交易所公開資訊觀測站（mopsov.twse.com.tw）｜全文檢索主旨與內文</p>
    </body></html>
    """


def send_email(announcements, report_markdown, attachment_path=None):
    password = os.environ.get("GMAIL_APP_PASSWORD")
    if not password:
        print("[!] 未設定 GMAIL_APP_PASSWORD，跳過發信")
        return False
    if not EMAIL_SENDER:
        print("[!] 未設定 EMAIL_SENDER（寄件帳號），跳過發信")
        return False
    if not EMAIL_RECEIVERS:
        print("[!] 未設定 EMAIL_RECEIVERS（收件人），跳過發信")
        return False

    today = now_tw().strftime("%Y-%m-%d")
    if announcements:
        subject = f"【MOPS 資安重訊】{today} 共 {len(announcements)} 筆公告"
    else:
        subject = f"【MOPS 資安重訊】{today} 無資安事件"

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = EMAIL_SENDER
    msg["To"] = ", ".join(EMAIL_RECEIVERS)

    body = MIMEMultipart("alternative")
    body.attach(MIMEText(report_markdown, "plain", "utf-8"))
    body.attach(MIMEText(generate_html_email(announcements), "html", "utf-8"))
    msg.attach(body)

    # 附加分析簡報 PPT
    if attachment_path and Path(attachment_path).exists():
        with open(attachment_path, "rb") as f:
            part = MIMEBase("application",
                            "vnd.openxmlformats-officedocument.presentationml.presentation")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        fname = f"資安事件因應分析_{today}.pptx"
        part.add_header("Content-Disposition", "attachment",
                        filename=("utf-8", "", fname))
        msg.attach(part)
        print(f"[*] 已附加簡報：{fname}")

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_SENDER, password)
            server.sendmail(EMAIL_SENDER, EMAIL_RECEIVERS, msg.as_string())
        print(f"[✓] 已發送 email 至 {len(EMAIL_RECEIVERS)} 位收件人：{', '.join(EMAIL_RECEIVERS)}")
        return True
    except Exception as e:
        print(f"[!] 發信失敗: {e}")
        return False


def save_report(markdown_content):
    reports_dir = Path("reports")
    reports_dir.mkdir(exist_ok=True)
    today = now_tw().strftime("%Y-%m-%d")
    report_file = reports_dir / f"{today}-isec-report.md"
    with open(report_file, "w", encoding="utf-8") as f:
        f.write(markdown_content)
    print(f"[✓] 報告已保存: {report_file}")
    return report_file


def git_commit_push(report_file):
    try:
        subprocess.run(["git", "config", "user.email", "automation@mops-monitor.local"], check=True)
        subprocess.run(["git", "config", "user.name", "MOPS Monitor"], check=True)
        subprocess.run(["git", "add", str(report_file)], check=True)
        if PREV_FILE.exists():
            subprocess.run(["git", "add", str(PREV_FILE)], check=True)
        if PPTX_FILE.exists():
            subprocess.run(["git", "add", str(PPTX_FILE)], check=True)
        today = now_tw().strftime("%Y-%m-%d")
        result = subprocess.run(
            ["git", "commit", "-m", f"MOPS report: {today}"],
            capture_output=True, text=True
        )
        if "nothing to commit" in result.stdout:
            print("[*] 報告已存在，略過 commit")
            return
        subprocess.run(["git", "push", "origin", "main"], check=True)
        print("[✓] 已推送到 GitHub")
    except subprocess.CalledProcessError as e:
        print(f"[!] Git 失敗: {e}")


def main():
    print("=" * 60)
    print("MOPS 資安重訊監控系統")
    print("=" * 60)

    try:
        # 查詢天數：預設 30 天，可用環境變數 QUERY_DAYS 調整
        query_days = int(os.environ.get("QUERY_DAYS", "30"))

        print(f"\n[步驟 1] 全文檢索過去 {query_days} 天的資安重訊...")
        announcements = scrape_mops_announcements(days=query_days)
        print(f"\n[結果] 找到 {len(announcements)} 筆資安相關公告")

        print("\n[步驟 2] 生成報告...")
        report = generate_report(announcements)

        print("\n[步驟 3] 保存報告...")
        report_file = save_report(report)

        print("\n[步驟 4] 產生分析簡報 PPT...")
        pptx_path = None
        if announcements:
            try:
                from ppt_generator import build_pptx
                report_date = now_tw().strftime("%Y-%m-%d")
                period_start = (now_tw() - timedelta(days=query_days)).strftime("%Y-%m-%d")
                pptx_path = build_pptx(announcements, str(PPTX_FILE), report_date, period_start)
                print(f"[✓] 簡報已產生：{pptx_path}")
            except Exception as e:
                print(f"[!] 產生簡報失敗: {e}")
                import traceback
                traceback.print_exc()
        else:
            print("[*] 無資安事件，略過簡報")

        print("\n[步驟 5] 發送 Email...")
        send_email(announcements, report, attachment_path=pptx_path)

        if announcements:
            print("\n[步驟 6] 提交到 GitHub...")
            git_commit_push(report_file)
        else:
            print("\n[步驟 6] 無資安事件，略過 GitHub 提交")

        print("\n" + "=" * 60)
        print("[✓] 完成！")
        print("=" * 60)

    except Exception as e:
        print(f"\n[✗] 錯誤: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
