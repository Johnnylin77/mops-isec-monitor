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
import time
import json
import smtplib
from urllib.parse import quote
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from pathlib import Path
import subprocess

import requests
from bs4 import BeautifulSoup

if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# ─────────────────────────── 設定 ───────────────────────────
EMAIL_SENDER = "abcd830428@gmail.com"
EMAIL_RECEIVER = "abcd830428@gmail.com"

EZSEARCH_URL = "https://mopsov.twse.com.tw/mops/web/ezsearch_query"
AUDITOR_URL = "https://mopsov.twse.com.tw/mops/web/ajax_t05st03"

# 記錄歷史上搜尋過的公司代號（用於標記 NEW）
SEEN_FILE = Path("reports") / "seen_companies.json"

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


def load_seen_companies():
    """讀取歷史上搜尋過的公司代號集合"""
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def save_seen_companies(codes):
    """儲存已搜尋過的公司代號集合"""
    SEEN_FILE.parent.mkdir(exist_ok=True)
    SEEN_FILE.write_text(
        json.dumps(sorted(codes), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def scrape_mops_announcements(days=30):
    """爬取過去 N 天內、符合資安關鍵字的重大訊息"""
    today = datetime.now()
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

    # 標記 NEW（與歷史搜尋過的公司比對）+ 補上簽證會計師資訊
    seen_codes = load_seen_companies()
    auditor_cache = {}
    print(f"\n[*] 標記 NEW 並查詢簽證會計師（歷史已知 {len(seen_codes)} 家）...")
    for ann in announcements:
        code = ann["code"]
        ann["is_new"] = code not in seen_codes

        if code not in auditor_cache:
            firm, cpas = get_auditor(code, session)
            auditor_cache[code] = (firm, cpas)
            time.sleep(0.2)
        firm, cpas = auditor_cache[code]
        ann["auditor_firm"] = firm
        ann["auditor_cpas"] = cpas
        tag = " 🆕" if ann["is_new"] else ""
        print(f"  {ann['company']} ({code}){tag} - {firm} / {'、'.join(cpas)}")

    # 更新歷史公司清單（把這次出現的都加進去）
    seen_codes.update(a["code"] for a in announcements)
    save_seen_companies(seen_codes)

    return announcements


def generate_report(announcements):
    today = datetime.now().strftime("%Y-%m-%d")
    one_month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

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
    today = datetime.now().strftime("%Y-%m-%d")
    one_month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

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


def send_email(announcements, report_markdown):
    password = os.environ.get("GMAIL_APP_PASSWORD")
    if not password:
        print("[!] 未設定 GMAIL_APP_PASSWORD，跳過發信")
        return False

    today = datetime.now().strftime("%Y-%m-%d")
    if announcements:
        subject = f"【MOPS 資安重訊】{today} 共 {len(announcements)} 筆公告"
    else:
        subject = f"【MOPS 資安重訊】{today} 無資安事件"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = EMAIL_SENDER
    msg["To"] = EMAIL_RECEIVER
    msg.attach(MIMEText(report_markdown, "plain", "utf-8"))
    msg.attach(MIMEText(generate_html_email(announcements), "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_SENDER, password)
            server.sendmail(EMAIL_SENDER, EMAIL_RECEIVER, msg.as_string())
        print(f"[✓] 已發送 email 至 {EMAIL_RECEIVER}")
        return True
    except Exception as e:
        print(f"[!] 發信失敗: {e}")
        return False


def save_report(markdown_content):
    reports_dir = Path("reports")
    reports_dir.mkdir(exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
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
        if SEEN_FILE.exists():
            subprocess.run(["git", "add", str(SEEN_FILE)], check=True)
        today = datetime.now().strftime("%Y-%m-%d")
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

        print("\n[步驟 4] 發送 Email...")
        send_email(announcements, report)

        if announcements:
            print("\n[步驟 5] 提交到 GitHub...")
            git_commit_push(report_file)
        else:
            print("\n[步驟 5] 無資安事件，略過 GitHub 提交")

        print("\n" + "=" * 60)
        print("[✓] 完成！")
        print("=" * 60)

    except Exception as e:
        print(f"\n[✗] 錯誤: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
