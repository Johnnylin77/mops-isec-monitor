# MOPS 資安重訊監控系統

每日自動監控台灣公開資訊觀測站（MOPS）上市櫃公司發布的**資訊安全相關重大訊息**，
比對主旨與內文關鍵字，找出資安事件後寄送 Email 報告並存檔。

## 功能特色

- 🔍 **全文檢索**：透過 MOPS「公告快易查」(ezsearch) 比對重大訊息的**主旨 + 內文**
- 📧 **Email 通知**：有資安事件寄送表格報告；無事件也寄通知
- 🆕 **NEW 標記**：與前一次報告比對，標示**新出現的資安事件**
- 👤 **簽證會計師**：每家公司附上目前的簽證會計師事務所與會計師姓名
- 🤖 **每日自動執行**：GitHub Actions 排程，台灣時間每天 08:00

## 監控關鍵字

`資安事件`、`資安緊急應變`、`資訊系統異常`、`資通系統異常`、`駭客攻擊`、
`攻擊事件`、`網路攻擊`、`駭客網路攻擊`、`入侵`、`異常通報`、`加密攻擊`

## 報表位置

每日報表存放於 `reports/` 資料夾，檔名格式 `YYYY-MM-DD-isec-report.md`。

## 手動觸發（含手機）

除了每天自動執行外，隨時可手動觸發一次（需先登入有本 repo 權限的 GitHub 帳號）：

### 方法一：瀏覽器（電腦 / 手機皆可）

1. 開啟 [Actions › MOPS 資安重訊監控](../../actions/workflows/monitor.yml)
2. 點右上「**Run workflow**」→ 再點綠色「**Run workflow**」
3. 約 1 分鐘後，信箱即收到報告

> 手機若按鈕不好點，可在瀏覽器選單切換到「電腦版網站 / Desktop site」。

### 方法二：GitHub 官方 App

1. 安裝並登入 **GitHub** App
2. 進入本 repo → **Actions** 分頁
3. 選「MOPS 資安重訊監控」→ 右上選單「**Run workflow**」

手動觸發與每日自動執行並存，跑的是完全相同的流程。

## 本機執行

```bash
pip install -r requirements.txt
# 設定寄信用的 Gmail 應用程式密碼（環境變數）
export GMAIL_APP_PASSWORD="你的_App_Password"   # Windows: set GMAIL_APP_PASSWORD=...
# 可選：調整查詢天數（預設 30）
export QUERY_DAYS=30
python mops_scraper.py
```

## 設定說明

| 項目 | 位置 |
|------|------|
| Gmail App Password | GitHub repo → Settings → Secrets → `GMAIL_APP_PASSWORD` |
| 寄件 / 收件信箱 | `mops_scraper.py` 內 `EMAIL_SENDER` / `EMAIL_RECEIVER` |
| 執行時間 | `.github/workflows/monitor.yml` 的 cron（預設 UTC 00:00 = 台灣 08:00）|
| 查詢天數 | 環境變數 `QUERY_DAYS`（預設 30 天）|

## 資料來源

台灣證券交易所公開資訊觀測站：https://mops.twse.com.tw

---

本系統由 Claude Code 協助開發與自動化排程驅動。
