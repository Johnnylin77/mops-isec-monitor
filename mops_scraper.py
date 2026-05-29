#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MOPS 資安重訊監控系統"""

import asyncio
import sys
import io
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from pathlib import Path
import subprocess

if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

from playwright.async_api import async_playwright

EMAIL_SENDER = "abcd830428@gmail.com"
EMAIL_RECEIVER = "abcd830428@gmail.com"

SECURITY_KEYWORDS = ["資訊安全", "資安", "遭駭", "個資外洩", "資安事件", "勒索", "網路攻擊", "資料外洩", "駭客"]


async def query_day_announcements(page, roc_year, month, day):
    """查詢舊版 MOPS 特定日期的所有重大訊息"""
    results = []
    try:
        await page.goto(
            "https://mopsov.twse.com.tw/mops/web/t05st02",
            timeout=30000,
            wait_until="domcontentloaded",
        )
        await page.wait_for_timeout(1500)

        await page.locator("#year").fill(str(roc_year))
        await page.select_option("#month", str(month))
        await page.select_option("#day", str(day))

        # 點「查詢」按鈕（不是 rulesubmit 搜尋按鈕）
        btns = await page.query_selector_all("input[type='button']")
        clicked = False
        for b in btns:
            val = (await b.get_attribute("value") or "").strip()
            bid = await b.get_attribute("id") or ""
            if "查詢" in val and bid != "rulesubmit":
                await b.click()
                clicked = True
                break

        if not clicked:
            print(f"  [!] 找不到查詢按鈕")
            return results

        # 等待 AJAX 回應
        try:
            await page.wait_for_function(
                "document.querySelectorAll('#div01 table tr').length > 3",
                timeout=12000,
            )
        except Exception:
            await page.wait_for_timeout(5000)

        rows = await page.query_selector_all("#div01 table tr")
        for row in rows:
            cells = await row.query_selector_all("td")
            if len(cells) < 5:
                continue
            texts = [(await c.text_content() or "").strip() for c in cells[:5]]
            date_str, time_str, code, company, title = texts[0], texts[1], texts[2], texts[3], texts[4]

            # 過濾掉表頭、空行
            if not code or not title or len(code) > 6:
                continue
            # 確認是數字代號
            if not code.isdigit():
                continue

            results.append({
                "date": date_str,
                "time": time_str,
                "code": code,
                "company": company,
                "title": title,
            })

    except Exception as e:
        print(f"  [!] 查詢 {roc_year}/{month}/{day} 失敗: {e}")

    return results


async def scrape_mops_announcements(days=30):
    """爬取過去 N 天的 MOPS 重大訊息，過濾資安相關"""
    security_announcements = []
    seen = set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )

        today = datetime.now()
        for i in range(days):
            target = today - timedelta(days=i)
            roc_year = target.year - 1911
            month = target.month
            day = target.day

            print(f"[*] 查詢 {roc_year}/{month:02d}/{day:02d}...", end=" ", flush=True)
            daily = await query_day_announcements(page, roc_year, month, day)
            print(f"{len(daily)} 筆")

            for ann in daily:
                # 關鍵字過濾
                matched = [kw for kw in SECURITY_KEYWORDS if kw in ann["title"]]
                if not matched:
                    continue

                key = f"{ann['code']}-{ann['date']}-{ann['title']}"
                if key in seen:
                    continue
                seen.add(key)

                ann["keyword"] = "、".join(matched)
                security_announcements.append(ann)
                print(f"  [✓] {ann['company']} ({ann['code']}) - {ann['title'][:50]}")

        await browser.close()

    return security_announcements


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
        markdown += f"""## {ann['company']} ({ann['code']}) - {ann['title']}

**發布日期：** {ann['date']} {ann.get('time','')}
**公司代號：** {ann['code']}
**符合關鍵字：** {ann['keyword']}

---

"""
    return markdown


def generate_html_email(announcements):
    today = datetime.now().strftime("%Y-%m-%d")
    one_month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

    if not announcements:
        return f"""
        <html><body style="font-family:Arial,sans-serif;">
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
        rows_html += f"""
        <tr>
            <td style="padding:8px;border:1px solid #ddd;">{ann['company']}</td>
            <td style="padding:8px;border:1px solid #ddd;">{ann['code']}</td>
            <td style="padding:8px;border:1px solid #ddd;">{ann['date']} {ann.get('time','')}</td>
            <td style="padding:8px;border:1px solid #ddd;">{ann['title']}</td>
            <td style="padding:8px;border:1px solid #ddd;">{ann['keyword']}</td>
        </tr>"""

    return f"""
    <html><body style="font-family:Arial,sans-serif;">
    <h2 style="color:#2c3e50;">MOPS 資安重訊監控報告</h2>
    <p><b>報告日期：</b>{today}<br>
    <b>資料期間：</b>{one_month_ago} 至 {today}<br>
    <b>公告數量：</b>{len(announcements)} 筆</p>
    <table style="border-collapse:collapse;width:100%;font-size:13px;">
        <tr style="background:#2c3e50;color:white;">
            <th style="padding:10px;border:1px solid #ddd;">公司名稱</th>
            <th style="padding:10px;border:1px solid #ddd;">代號</th>
            <th style="padding:10px;border:1px solid #ddd;">日期/時間</th>
            <th style="padding:10px;border:1px solid #ddd;">標題</th>
            <th style="padding:10px;border:1px solid #ddd;">符合關鍵字</th>
        </tr>
        {rows_html}
    </table>
    <br><p style="color:gray;font-size:12px;">資料來源：台灣證券交易所公開資訊觀測站（mopsov.twse.com.tw）</p>
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


async def main():
    print("=" * 60)
    print("MOPS 資安重訊監控系統")
    print("=" * 60)

    try:
        # 首次執行查30天；日常運行可改小（例如2天）
        query_days = int(os.environ.get("QUERY_DAYS", "30"))

        print(f"\n[步驟 1] 爬取過去 {query_days} 天的公告...")
        announcements = await scrape_mops_announcements(days=query_days)
        print(f"\n[結果] 找到 {len(announcements)} 筆資安相關公告")

        print("\n[步驟 2] 生成報告...")
        report = generate_report(announcements)

        print("\n[步驟 3] 保存報告...")
        report_file = save_report(report)

        print("\n[步驟 4] 發送 Email...")
        send_email(announcements, report)

        # 有資料才提交報告到 GitHub
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
    asyncio.run(main())
