# Little_Shijimi

Little_Shijimi 是一個給 Codex 用的本機 Telegram 通知 wrapper。

目前版本：`v1.0.0`，發布日 `2026-04-09`

這個版本把專案從單向通知器升級成可遠端回應 approval 的橋接層：你現在可以直接在 Telegram 回覆 Codex 的 approval prompt，wrapper 會把輸入送回正確 terminal，另外 repo 也附上了離線 replay tester 用來驗證整條流程。

## 版本重點

- 把 Telegram 的 `go` 與 `no` 回覆送回對應的本機 terminal，處理 Codex approval prompt。
- 在互動式 wrapper session 中自動啟動背景 reply listener。
- 根據畫面上實際出現的 approval 選項，自動推導 `Enter` 或方向鍵的輸入序列。
- 新增 replay tester，可用歷史 prompt 驗證 approval reply routing，不碰 live 工作 session。
- `install.sh` 會把 `~/.local/bin/codex` 做成指向 `wrappers/codex` 的 symlink，讓本機安裝和 repo 副本保持同步。

更新紀錄請看 `CHANGELOG.md`，GitHub release 內容來源請看 `releases/v1.0.0.md`。

它會包住真正的 `codex` binary，監看互動 session 檔、辨識卡住等待輸入的 prompt，並在以下情況發送簡短 Telegram 訊息：

- Codex 在 session 中產生 final answer
- Codex 要求批准 escalated command
- 你在 Telegram 對 approval prompt 回 `go` 或 `no`，wrapper 會把輸入送回對應 terminal
- Codex 在執行關卡等待使用者輸入
- `codex exec` 或其他非 TTY 執行結束，且有成功擷取最後一則訊息

## 專案內容

- `wrappers/codex`：放在真正 Codex 前面的 shell wrapper
- `scripts/agent_telegram_tty_proxy.py`：互動模式下的 PTY prompt 偵測代理
- `scripts/codex_session_watch.py`：背景監看 `~/.codex/sessions/**/*.jsonl`
- `scripts/send_last_message.py`：處理非互動模式最後訊息發送
- `scripts/telegram_common.py`：共用的 Telegram send/delete helper
- `scripts/telegram_reply_listener.py`：輪詢 Telegram reply，並把 go/no 動作寫到對應 terminal 的 control file
- `scripts/telegram_approval_replay_tester.py`：離線 replay tester，會重播歷史 approval prompt 並驗證 reply routing，不碰 live 工作 session
- `env/codex-telegram.env.example`：環境變數範例
- `install.sh`：本機安裝腳本

## 運作方式

互動模式：

- wrapper 先啟動背景 session watcher
- wrapper 也會啟動背景 Telegram reply listener
- 真正的 Codex 透過 PTY prompt proxy 執行
- proxy 會偵測 approval prompt 與 execution gate
- approval prompt 會用目前 terminal id 註冊 pending 狀態，Telegram 訊息也會提示回 `go` 或 `no`
- reply listener 會把 Telegram reply 配對到 pending prompt，然後把對應按鍵序列寫回正確 terminal 的 control file
- 對 approval 選單來說，proxy 會根據畫面上的選項順序推導 `Enter` 或 `方向鍵 + Enter`，預設假設第一個選項有焦點；如果你的本機 UI 起始焦點不同，可用 `AGENT_NOTIFY_APPROVAL_START_INDEX` 覆寫
- watcher 會 tail 對應的 `~/.codex/sessions/**/*.jsonl`，抓 final answer 或 approval 類事件後送出 Telegram

非互動模式：

- 如果命令包含 `exec`，而且尚未自行提供 `--output-last-message`，wrapper 會自動補上
- 命令結束後，把擷取到的最後訊息送到 Telegram

## 需要的環境變數

必要：

- `TG_BOT_TOKEN`
- `TG_CHAT_ID`

常用可選：

- `CODEX_NOTIFY_REAL_CODEX`：真正的 Codex binary 路徑，預設 `/opt/homebrew/bin/codex`
- `CODEX_NOTIFY_DELETE_AFTER`：訊息送出後幾秒自動刪除
- `CODEX_NOTIFY_PREVIEW_CHARS`：debug log 預覽字數
- `CODEX_NOTIFY_ATTACH_TIMEOUT`：watcher 附著 session 的等待秒數
- `CODEX_NOTIFY_REPLY_LISTENER`：覆寫 Telegram reply listener 腳本路徑
- `CODEX_NOTIFY_REPLY_IDLE_EXIT`：reply listener 閒置多久後自動退出
- `CODEX_NOTIFY_REPLY_POLL_TIMEOUT`：Telegram long poll 的等待秒數
- `CODEX_NOTIFY_LOG`：log 路徑
- `CODEX_NOTIFY_STATE_DIR`：state 與 delete queue 目錄
- `CODEX_NOTIFY_LABEL`：Telegram 標頭顯示的 label
- `CODEX_NOTIFY_DISABLE=1`：直接略過 wrapper，原樣執行 Codex
- `AGENT_NOTIFY_APPROVAL_START_INDEX`：若你的 approval UI 起始焦點不是第一個選項，可用這個值覆寫

## 安裝

```bash
cd Little_Shijimi
./install.sh
```

然後編輯 `~/.config/codex-telegram.env`。

安裝腳本會把 `~/.local/bin/codex` 連成指向這個 repo 的 `wrappers/codex` 的 symlink；只要 repo 沒搬位置，之後你在 repo 內改的內容就會直接反映到本機使用版本。

在 shell rc 裡加入：

```bash
if [[ -f "$HOME/.config/codex-telegram.env" ]]; then
  source "$HOME/.config/codex-telegram.env"
fi

codex() {
  command ~/.local/bin/codex "$@"
}
```

## 離線 Replay 測試

可以直接跑 repo 內的 approval-reply replay tester：

```bash
cd Little_Shijimi
python3 scripts/telegram_approval_replay_tester.py
```

它會從 `~/.codex/sessions` 找一條歷史 approval prompt，用假的 child process 與假的 Telegram `go` reply 做整條驗證，確認輸入會打到正確 terminal id，不需要碰 live 工作 session。

## Update Log

- `2026-04-09`：`v1.0.0` 新增 Telegram approval reply routing、背景 reply listener、可 mock 的 Telegram API helper，以及離線 approval replay tester。

## 備註

- 這個專案只用 Python 標準函式庫。
- Telegram 憑證從環境變數讀取，不要把真實 token 提交到 repo。
- 目前專案焦點是 Codex；雖然 prompt proxy 的 regex 也能吃 Claude 類型的 approval 文字，但 repo 這版只附 Codex wrapper。

## 授權

MIT
