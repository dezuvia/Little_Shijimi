# Little_Shijimi

Little_Shijimi 是一個給 Codex 用的本機 Telegram 通知 wrapper。

它會包住真正的 `codex` binary，監看互動 session 檔、辨識卡住等待輸入的 prompt，並在以下情況發送簡短 Telegram 訊息：

- Codex 在 session 中產生 final answer
- Codex 要求批准 escalated command
- Codex 在執行關卡等待使用者輸入
- `codex exec` 或其他非 TTY 執行結束，且有成功擷取最後一則訊息

## 專案內容

- `wrappers/codex`：放在真正 Codex 前面的 shell wrapper
- `scripts/agent_telegram_tty_proxy.py`：互動模式下的 PTY prompt 偵測代理
- `scripts/codex_session_watch.py`：背景監看 `~/.codex/sessions/**/*.jsonl`
- `scripts/send_last_message.py`：處理非互動模式最後訊息發送
- `scripts/telegram_common.py`：共用的 Telegram send/delete helper
- `env/codex-telegram.env.example`：環境變數範例
- `install.sh`：本機安裝腳本

## 運作方式

互動模式：

- wrapper 先啟動背景 session watcher
- 真正的 Codex 透過 PTY prompt proxy 執行
- proxy 會偵測 approval prompt 與 execution gate
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
- `CODEX_NOTIFY_LOG`：log 路徑
- `CODEX_NOTIFY_STATE_DIR`：state 與 delete queue 目錄
- `CODEX_NOTIFY_LABEL`：Telegram 標頭顯示的 label
- `CODEX_NOTIFY_DISABLE=1`：直接略過 wrapper，原樣執行 Codex

## 安裝

```bash
cd Little_Shijimi
./install.sh
```

然後編輯 `~/.config/codex-telegram.env`。

在 shell rc 裡加入：

```bash
if [[ -f "$HOME/.config/codex-telegram.env" ]]; then
  source "$HOME/.config/codex-telegram.env"
fi

codex() {
  command ~/.local/bin/codex "$@"
}
```

## 備註

- 這個專案只用 Python 標準函式庫。
- Telegram 憑證從環境變數讀取，不要把真實 token 提交到 repo。
- 目前專案焦點是 Codex；雖然 prompt proxy 的 regex 也能吃 Claude 類型的 approval 文字，但 repo 這版只附 Codex wrapper。

## 授權

MIT
