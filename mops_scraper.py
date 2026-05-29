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

# Email 設定（密碼由環境變數傳入，勿寫死於此）
EMAIL_SENDER = "abcd830428@gmail.com"
EMAIL_RECEIVER = "abcd830428@gmail.com"


def is_recent_announcement(date_str):
    """民國年日期 → 是否在過去30天內"""
    try:
        parts = date_str.strip().split()[0].split("/")
        if len(parts) >= 3:
            roc_year = int(parts[0])
            month = int(parts[1])
            day = int(parts[2])
            announce_date = datetime(roc_year + 1911, month, day)
            return announce_date >= datetime.now() - timedelta(days=30)
    except Exception as e:
        print(f"[!] 日期解析失敗: {date_str} - {e}")
    return False


async def scrape_mops_announcements():
    """使用 Playwright 爬取 MOPS 資安重訊（t05st02 關鍵字搜尋）"""
    keywords = ["資訊安全", "遭駭", "個資外洩", "資安事件", "勒索", "網路攻擊"]
    announcements = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
        page = await context.new_page()

        for keyword in keywords:
            try:
                print(f"\n[*] 搜尋關鍵字: {keyword}")

                # 正確頁面：重大訊息「內容」關鍵字查詢
                await page.goto(
                    "https://mops.twse.com.tw/mops/web/t05st02",
                    timeout=30000,
                    wait_until="domcontentloaded",
                )
                await page.wait_for_timeout(1500)

                # 除錯：第一次迴圈截圖並列出所有 input
                if keyword == keywords[0]:
                    await page.screenshot(path="debug_page.png")
                    inputs = await page.query_selector_all("input[type=text], input:not([type])")
                    print(f"[debug] 找到 {len(inputs)} 個文字輸入框")
                    for inp in inputs:
                        inp_id = await inp.get_attribute("id")
                        inp_name = await inp.get_attribute("name")
                        print(f"  id={inp_id}, name={inp_name}")

                # t05st02 搜尋欄位，多個 selector 作為 fallback
                search_box = (
                    await page.query_selector("#key_word")
                    or await page.query_selector("input[name='key_word']")
                    or await page.query_selector("input[name='KEYWORD']")
                    or await page.query_selector("input[type='text']")
                )

                if not search_box:
                    print("[!] 找不到搜尋框，跳過")
                    await page.screenshot(path=f"debug_{keyword}.png")
                    continue

                await search_box.fill(keyword)

                # 優先點擊查詢按鈕，比 Enter 更可靠
                submit_btn = (
                    await page.query_selector("input[type='submit']")
                    or await page.query_selector("button[type='submit']")
                    or await page.query_selector("input[value='查詢']")
                    or await page.query_selector("input[value='Search']")
                )

                if submit_btn:
                    await submit_btn.click()
                else:
                    await page.keyboard.press("Enter")

                # 等待結果表格出現
                try:
                    await page.wait_for_selector("table", timeout=10000)
                except Exception:
                    await page.wait_for_load_state("networkidle", timeout=10000)
                await page.wait_for_timeout(1000)

                rows = await page.query_selector_all("table tr")
                print(f"[*] 找到 {len(rows)} 行")

                for i, row in enumerate(rows):
                    if i == 0:
                        continue  # 表頭

                    cells = await row.query_selector_all("td")
                    if len(cells) < 4:
                        continue

                    try:
                        cell_texts = []
                        for cell in cells[:5]:
                            text = await cell.text_content()
                            cell_texts.append(text.strip() if text else "")

                        # 欄位：序號 / 公司代號 / 公司名稱 / 日期 / 標題（5欄）
                        # 或：公司代號 / 公司名稱 / 日期 / 標題（4欄）
                        if len(cell_texts) >= 5:
                            code, company, date_str, title = (
                                cell_texts[1], cell_texts[2], cell_texts[3], cell_texts[4]
                            )
                        else:
                            code, company, date_str, title = (
                                cell_texts[0], cell_texts[1], cell_texts[2], cell_texts[3]
                            )

                        if not code or "查無" in title or "查無" in code:
                            continue

                        if not is_recent_announcement(date_str):
                            continue

                        if not any(
                            a["code"] == code and a["date"] == date_str and a["title"] == title
                            for a in announcements
                        ):
                            announcements.append({
                                "code": code,
                                "company": company,
                                "date": date_str,
                                "title": title,
                                "keyword": keyword,
                            })
                            print(f"[✓] {company} ({code}) - {title[:50]}")

                    except Exception as e:
                        print(f"[!] 解析行失敗: {e}")

            except Exception as e:
                print(f"[!] 搜尋 '{keyword}' 失敗: {e}")
                await page.screenshot(path=f"debug_error_{keyword}.png")

        await browser.close()

    return announcements


def generate_report(announcements):
    today = datetime.now().strftime("%Y-%m-%d")
    one_month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

    if not announcements:
        return None

    markdown = f"""# MOPS 資安重訊監控報告

**報告日期：** {today}
**資料期間：** {one_month_ago} 至 {today}
**資料來源：** 台灣證券交易所公開資訊觀測站
**公告數量：** {len(announcements)}

---

"""
    for ann in announcements:
        markdown += f"""## {ann['company']} ({ann['code']}) - {ann['title']}

**發布日期：** {ann['date']}
**公司代號：** {ann['code']}
**搜尋關鍵字：** {ann['keyword']}

---

"""
    return markdown


def generate_html_email(announcements):
    """生成 HTML 格式信件內容"""
    today = datetime.now().strftime("%Y-%m-%d")
    one_month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

    rows_html = ""
    for ann in announcements:
        rows_html += f"""
        <tr>
            <td style="padding:8px;border:1px solid #ddd;">{ann['company']}</td>
            <td style="padding:8px;border:1px solid #ddd;">{ann['code']}</td>
            <td style="padding:8px;border:1px solid #ddd;">{ann['date']}</td>
            <td style="padding:8px;border:1px solid #ddd;">{ann['title']}</td>
            <td style="padding:8px;border:1px solid #ddd;">{ann['keyword']}</td>
        </tr>"""

    html = f"""
    <html><body>
    <h2>MOPS 資安重訊監控報告</h2>
    <p><b>報告日期：</b>{today}<br>
    <b>資料期間：</b>{one_month_ago} 至 {today}<br>
    <b>公告數量：</b>{len(announcements)} 筆</p>
    <table style="border-collapse:collapse;width:100%;">
        <tr style="background:#4472C4;color:white;">
            <th style="padding:8px;border:1px solid #ddd;">公司名稱</th>
            <th style="padding:8px;border:1px solid #ddd;">代號</th>
            <th style="padding:8px;border:1px solid #ddd;">日期</th>
            <th style="padding:8px;border:1px solid #ddd;">標題</th>
            <th style="padding:8px;border:1px solid #ddd;">關鍵字</th>
        </tr>
        {rows_html}
    </table>
    <br><p style="color:gray;font-size:12px;">資料來源：台灣證券交易所公開資訊觀測站</p>
    </body></html>
    """
    return html


def send_email(announcements, report_markdown):
    """寄送監控結果到 Gmail"""
    password = os.environ.get("GMAIL_APP_PASSWORD")
    if not password:
        print("[!] 未設定 GMAIL_APP_PASSWORD 環境變數，跳過發信")
        return False

    today = datetime.now().strftime("%Y-%m-%d")
    subject = f"【MOPS 資安重訊】{today} 共 {len(announcements)} 筆公告"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = EMAIL_SENDER
    msg["To"] = EMAIL_RECEIVER

    # 純文字版本（fallback）
    msg.attach(MIMEText(report_markdown, "plain", "utf-8"))
    # HTML 版本
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
        subprocess.run(["git", "commit", "-m", f"MOPS report: {today}"], check=True)
        subprocess.run(["git", "push", "origin", "main"], check=True)
        print("[✓] 已推送到 GitHub")
    except subprocess.CalledProcessError as e:
        print(f"[!] Git 失敗: {e}")


async def main():
    print("=" * 60)
    print("MOPS 資安重訊監控系統")
    print("=" * 60)

    try:
        print("\n[步驟 1] 爬取公告...")
        announcements = await scrape_mops_announcements()
        print(f"\n[結果] 找到 {len(announcements)} 筆公告")

        if not announcements:
            print("[!] 未找到任何公告，停止執行")
            return

        print("\n[步驟 2] 生成報告...")
        report = generate_report(announcements)
        if not report:
            return

        print("\n[步驟 3] 保存報告...")
        report_file = save_report(report)

        print("\n[步驟 4] 發送 Email...")
        send_email(announcements, report)

        print("\n[步驟 5] 提交到 GitHub...")
        git_commit_push(report_file)

        print("\n" + "=" * 60)
        print("[✓] 完成！")
        print("=" * 60)

    except Exception as e:
        print(f"\n[✗] 錯誤: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
