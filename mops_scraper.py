#!/usr/bin/env python3
"""MOPS 資安重訊監控系統 - 主爬蟲腳本"""

import asyncio
import json
import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from playwright.async_api import async_playwright


async def scrape_mops_announcements():
    """使用 Playwright 爬取 MOPS 資安重訊"""

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        # 關鍵字清單
        keywords = ["資訊安全", "遭駭", "個資外洩", "資安事件"]
        announcements = []

        for keyword in keywords:
            try:
                print(f"搜尋關鍵字: {keyword}")

                # 訪問 MOPS 首頁
                await page.goto("https://mops.twse.com.tw/mops/web/index", timeout=30000)
                await page.wait_for_load_state("networkidle")

                # 切換到查詢頁面
                mops_url = "https://mops.twse.com.tw/mops/web/t05st01"
                await page.goto(mops_url, timeout=30000)
                await page.wait_for_load_state("networkidle")

                # 查找並填入搜尋框
                try:
                    search_input = await page.query_selector("input[type='text']")
                    if search_input:
                        await search_input.fill(keyword)
                        await page.keyboard.press("Enter")
                        await page.wait_for_load_state("networkidle")
                        await page.wait_for_timeout(2000)
                except Exception as e:
                    print(f"輸入關鍵字失敗: {e}")

                # 提取結果表格
                try:
                    rows = await page.query_selector_all("tr")
                    for row in rows:
                        cells = await row.query_selector_all("td")
                        if len(cells) >= 5:
                            try:
                                cells_text = []
                                for cell in cells[:6]:
                                    text = await cell.text_content()
                                    cells_text.append(text.strip() if text else "")

                                announcement = {
                                    "company": cells_text[0],
                                    "code": cells_text[1] if len(cells_text) > 1 else "",
                                    "date": cells_text[2] if len(cells_text) > 2 else "",
                                    "title": cells_text[3] if len(cells_text) > 3 else "",
                                    "content": cells_text[4] if len(cells_text) > 4 else "",
                                    "market": cells_text[5] if len(cells_text) > 5 else "",
                                    "keyword": keyword,
                                }

                                if announcement["market"] and ("上市" in announcement["market"] or "上櫃" in announcement["market"]):
                                    if is_recent_date(announcement["date"]):
                                        announcements.append(announcement)
                                        print(f"✓ 找到: {announcement['company']} - {announcement['title'][:50]}")
                            except Exception as e:
                                print(f"解析行失敗: {e}")

                except Exception as e:
                    print(f"提取表格失敗: {e}")

            except Exception as e:
                print(f"搜尋 {keyword} 失敗: {e}")

        await browser.close()
        return announcements


def is_recent_date(date_str):
    """檢查日期是否在過去一個月內"""
    try:
        announce_date = datetime.strptime(date_str, "%Y/%m/%d")
        one_month_ago = datetime.now() - timedelta(days=30)
        return announce_date >= one_month_ago
    except:
        return False


def generate_markdown_report(announcements):
    """生成 Markdown 報告"""

    today = datetime.now().strftime("%Y-%m-%d")
    one_month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

    unique_announcements = []
    seen = set()
    for ann in announcements:
        key = f"{ann['code']}-{ann['date']}-{ann['title']}"
        if key not in seen:
            unique_announcements.append(ann)
            seen.add(key)

    if not unique_announcements:
        print("⚠️  未找到符合條件的公告")
        return None

    markdown = f"""# MOPS 資安重訊監控報告

**報告日期：** {today}
**資料期間：** {one_month_ago} 至 {today}
**資料來源：** 台灣證券交易所公開資訊觀測站
**公告數量：** {len(unique_announcements)}

---

"""

    for ann in unique_announcements:
        markdown += f"""## {ann['company']} ({ann['code']}) - {ann['title']}

**發布日期：** {ann['date']}
**公司代號：** {ann['code']}
**市場別：** {ann['market']}
**重訊內容摘要：** {ann['content'][:200]}...

---

"""

    return markdown


def save_report(markdown_content):
    """保存報告到 reports 資料夾"""

    reports_dir = Path("reports")
    reports_dir.mkdir(exist_ok=True)

    today = datetime.now().strftime("%Y-%m-%d")
    report_file = reports_dir / f"{today}-isec-report.md"

    with open(report_file, "w", encoding="utf-8") as f:
        f.write(markdown_content)

    print(f"✓ 報告已保存: {report_file}")
    return report_file


def git_commit_and_push(report_file):
    """提交並推送到 GitHub"""

    try:
        subprocess.run(["git", "config", "user.email", "automation@mops-monitor.local"], check=True)
        subprocess.run(["git", "config", "user.name", "MOPS Monitor Bot"], check=True)

        subprocess.run(["git", "add", str(report_file)], check=True)

        today = datetime.now().strftime("%Y-%m-%d")
        commit_msg = f"Add MOPS security report for {today}"
        subprocess.run(["git", "commit", "-m", commit_msg], check=True)

        subprocess.run(["git", "push", "origin", "main"], check=True)

        print(f"✓ 已推送到 GitHub")

    except subprocess.CalledProcessError as e:
        print(f"⚠️  Git 操作失敗: {e}")


def send_email_report(report_file, markdown_content):
    """發送郵件報告 (使用 Claude 的 Gmail MCP connector)"""

    try:
        today = datetime.now().strftime("%Y-%m-%d")

        email_data = {
            "to": "abcd830428@gmail.com",
            "subject": f"MOPS 資安重訊監控報告 - {today}",
            "body": markdown_content,
            "report_file": str(report_file),
        }

        email_file = Path("email_data.json")
        with open(email_file, "w", encoding="utf-8") as f:
            json.dump(email_data, f, ensure_ascii=False, indent=2)

        print(f"✓ 郵件數據已準備: {email_file}")
        return email_data

    except Exception as e:
        print(f"⚠️  郵件準備失敗: {e}")
        return None


async def main():
    """主程序"""

    print("=" * 60)
    print("MOPS 資安重訊監控系統 - 啟動")
    print("=" * 60)

    try:
        print("\n[步驟 1] 爬取 MOPS 資安公告...")
        announcements = await scrape_mops_announcements()
        print(f"找到 {len(announcements)} 筆公告")

        if not announcements:
            print("⚠️  未找到任何公告，停止執行")
            return

        print("\n[步驟 2] 生成 Markdown 報告...")
        markdown_content = generate_markdown_report(announcements)

        if not markdown_content:
            print("⚠️  報告生成失敗，停止執行")
            return

        print("\n[步驟 3] 保存報告到本地...")
        report_file = save_report(markdown_content)

        print("\n[步驟 4] 提交到 GitHub...")
        git_commit_and_push(report_file)

        print("\n[步驟 5] 準備郵件報告...")
        send_email_report(report_file, markdown_content)

        print("\n" + "=" * 60)
        print("✓ 執行完成！")
        print("=" * 60)

    except Exception as e:
        print(f"\n✗ 執行出錯: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
