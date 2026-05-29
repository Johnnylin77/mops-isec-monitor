#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MOPS 資安重訊監控系統 - 改進版爬蟲"""

import asyncio
import json
import sys
import io
from datetime import datetime, timedelta
from pathlib import Path
import subprocess

if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

from playwright.async_api import async_playwright


async def scrape_mops_announcements():
    """使用 Playwright 爬取 MOPS 資安重訊"""

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        keywords = ["資訊安全", "遭駭", "個資外洩", "資安事件", "勒索", "網路攻擊"]
        announcements = []

        for keyword in keywords:
            try:
                print(f"\n[*] 搜尋關鍵字: {keyword}")

                # 訪問搜尋頁面
                await page.goto("https://mops.twse.com.tw/mops/web/t05st01", timeout=30000)
                await page.wait_for_load_state("networkidle")
                await page.wait_for_timeout(1000)

                # 清空並填入搜尋框
                search_box = await page.query_selector("#searchInfo")
                if search_box:
                    await search_box.fill("")
                    await search_box.fill(keyword)
                    await page.keyboard.press("Enter")
                    await page.wait_for_load_state("networkidle")
                    await page.wait_for_timeout(2000)

                    # 提取結果表格
                    rows = await page.query_selector_all("tr")
                    print(f"[*] 找到 {len(rows)} 行")

                    for i, row in enumerate(rows):
                        if i == 0:  # 跳過表頭
                            continue

                        cells = await row.query_selector_all("td")
                        if len(cells) >= 4:
                            try:
                                cell_texts = []
                                for j, cell in enumerate(cells[:4]):
                                    text = await cell.text_content()
                                    cell_texts.append(text.strip() if text else "")

                                code = cell_texts[0]
                                company = cell_texts[1]
                                date_str = cell_texts[2]
                                title = cell_texts[3]

                                # 過濾掉"查無資料"
                                if title == "查無資料" or not code or code == "查無資料":
                                    continue

                                # 檢查日期是否在過去一個月內
                                if not is_recent_announcement(date_str):
                                    continue

                                announcement = {
                                    "code": code,
                                    "company": company,
                                    "date": date_str,
                                    "title": title,
                                    "keyword": keyword,
                                }

                                # 檢查是否重複
                                key = f"{code}-{date_str}-{title}"
                                if not any(a.get("code") == code and a.get("date") == date_str 
                                          and a.get("title") == title for a in announcements):
                                    announcements.append(announcement)
                                    print(f"[✓] 找到: {company} ({code}) - {title[:50]}")

                            except Exception as e:
                                print(f"[!] 解析行失敗: {e}")

                else:
                    print("[!] 找不到搜尋框")

            except Exception as e:
                print(f"[!] 搜尋 '{keyword}' 失敗: {e}")

        await browser.close()
        return announcements


def is_recent_announcement(date_str):
    """檢查公告日期是否在過去一個月內"""
    try:
        # 日期格式: 115/05/29 07:00
        # 民國年 → 西元年
        parts = date_str.split()[0].split("/")
        if len(parts) >= 3:
            roc_year = int(parts[0])
            month = int(parts[1])
            day = int(parts[2])
            gregorian_year = roc_year + 1911
            
            announce_date = datetime(gregorian_year, month, day)
            one_month_ago = datetime.now() - timedelta(days=30)
            
            return announce_date >= one_month_ago
    except Exception as e:
        print(f"[!] 日期解析失敗: {date_str} - {e}")
    
    return False


def generate_report(announcements):
    """生成 Markdown 報告"""

    today = datetime.now().strftime("%Y-%m-%d")
    one_month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

    if not announcements:
        print("[!] 未找到任何符合條件的公告")
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


def save_report(markdown_content):
    """保存報告"""

    reports_dir = Path("reports")
    reports_dir.mkdir(exist_ok=True)

    today = datetime.now().strftime("%Y-%m-%d")
    report_file = reports_dir / f"{today}-isec-report.md"

    with open(report_file, "w", encoding="utf-8") as f:
        f.write(markdown_content)

    print(f"[✓] 報告已保存: {report_file}")
    return report_file


def git_commit_push(report_file):
    """提交到 GitHub"""

    try:
        subprocess.run(["git", "config", "user.email", "automation@mops-monitor.local"], check=True)
        subprocess.run(["git", "config", "user.name", "MOPS Monitor"], check=True)
        subprocess.run(["git", "add", str(report_file)], check=True)

        today = datetime.now().strftime("%Y-%m-%d")
        subprocess.run(["git", "commit", "-m", f"MOPS report: {today}"], check=True)
        subprocess.run(["git", "push", "origin", "main"], check=True)

        print(f"[✓] 已推送到 GitHub")

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

        print("\n[步驟 4] 提交到 GitHub...")
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
