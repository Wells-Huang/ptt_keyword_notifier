# PTT Keyword Notifier

每 5 分鐘檢查 PTT Gamesale 最新兩頁，尋找「真三國無雙 起源」的 Nintendo Switch 2 販售文章，並透過 Discord Webhook 通知。

## 行為

- 平台：NS2、Switch 2、Nintendo Switch 2。
- 販售標記：標題必須包含「售」。
- 靜默時段：台北時間 01:00（含）至 08:30（不含）。靜默時段仍抓取並保存 pending，08:30 後補送。
- data/state.json 保存已通知與待通知文章；不得清空 notified 以免重複通知。
- Discord payload 關閉 mentions，不會被 PTT 標題中的 @everyone 觸發。

## GitHub 設定

Repository Settings → Actions → General → Workflow permissions 必須允許 workflow 寫入 repository contents，因為 monitor workflow 只在狀態變更時提交 data/state.json 或每月 heartbeat。

Repository secret 必須存在：

    DISCORD_WEBHOOK_URL

Webhook URL 不得寫入 repository、issue、log 或 workflow output。

## 本機執行

本專案不需要安裝套件：

    python -m unittest discover -s tests -v
    python -m src.ptt_keyword_notifier --mode dry-run

測試 Discord（會實際發一則測試訊息）：

    $env:DISCORD_WEBHOOK_URL = '請只在本機程序環境設定，不要寫入檔案'
    python -m src.ptt_keyword_notifier --mode test-discord

## GitHub Actions 操作順序

1. 先 push 後確認 CI 綠燈。
2. Actions → Monitor PTT Gamesale → Run workflow → dry-run。
3. 再執行 test-discord，確認 Discord 收到測試訊息。
4. 執行一次 normal，確認既有 6 筆 state 不會重新通知。
5. 再執行一次 normal，確認沒有新 Discord 訊息，也沒有空 commit。
6. 觀察至少兩次自動 schedule run 後，才停用本機 Windows 排程。

GitHub schedule 是 best effort，可能延遲或偶發漏跑；Actions history 是主要診斷來源。
