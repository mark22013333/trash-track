#!/usr/bin/env bash

set -uo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$SCRIPT_DIR" || {
  echo "[錯誤] 無法進入專案目錄：$SCRIPT_DIR" >&2
  exit 1
}

LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR" || {
  echo "[錯誤] 無法建立 LOG 目錄：$LOG_DIR" >&2
  exit 1
}

TIMESTAMP="$(date '+%Y%m%d-%H%M%S')"
LOG_FILE="$LOG_DIR/server-$TIMESTAMP.log"
VENV_DIR="$SCRIPT_DIR/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"

log() {
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" | tee -a "$LOG_FILE"
}

fail() {
  log "[錯誤] $1"
  printf '\n請查看 LOG：%s\n' "$LOG_FILE" >&2
  exit 1
}

log "[啟動] TrashTrack 前置檢查開始"
log "[資訊] 專案目錄：$SCRIPT_DIR"
log "[資訊] 本次 LOG：$LOG_FILE"

if [[ -z "${GOOGLE_MAPS_API_KEY:-}" ]]; then
  log "[警告] 尚未設定 GOOGLE_MAPS_API_KEY；網站仍可查看路線列表，但不會顯示地圖。"
  log "[設定] 請先執行：export GOOGLE_MAPS_API_KEY=\"你的金鑰\""
else
  log "[通過] 已設定 Google Maps API 金鑰"
fi

if [[ -z "${GOOGLE_MAPS_MAP_ID:-}" ]]; then
  log "[警告] 尚未設定 GOOGLE_MAPS_MAP_ID；Advanced Marker 地圖將不會啟用。"
  log "[設定] 請先執行：export GOOGLE_MAPS_MAP_ID=\"你的 Map ID\""
else
  log "[通過] 已設定 Google Maps Map ID"
fi

[[ -f "$SCRIPT_DIR/app.py" ]] || fail "找不到 app.py，請確認腳本位於專案根目錄。"
[[ -f "$SCRIPT_DIR/requirements.txt" ]] || fail "找不到 requirements.txt。"

SYSTEM_PYTHON=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 \
    && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
    SYSTEM_PYTHON="$candidate"
    break
  fi
done

if [[ -z "$SYSTEM_PYTHON" ]]; then
  fail "找不到 Python 3.9 以上版本。請先安裝 Python 3，再重新執行此腳本。"
fi

PYTHON_VERSION="$($SYSTEM_PYTHON --version 2>&1)"
log "[通過] 已找到 ${PYTHON_VERSION}（${SYSTEM_PYTHON}）"

if [[ ! -x "$VENV_PYTHON" ]]; then
  if [[ -d "$VENV_DIR" ]]; then
    fail ".venv 已存在但不完整。請將它改名或刪除後再執行。"
  fi
  log "[設定] 正在建立虛擬環境 .venv…"
  "$SYSTEM_PYTHON" -m venv "$VENV_DIR" 2>&1 | tee -a "$LOG_FILE"
  create_status=${PIPESTATUS[0]}
  [[ $create_status -eq 0 && -x "$VENV_PYTHON" ]] \
    || fail "虛擬環境建立失敗。請確認 Python 的 venv 模組可用。"
else
  log "[通過] 已找到既有虛擬環境 .venv"
fi

if ! "$VENV_PYTHON" -m pip --version >/dev/null 2>&1; then
  log "[設定] 虛擬環境缺少 pip，正在修復…"
  "$VENV_PYTHON" -m ensurepip --upgrade 2>&1 | tee -a "$LOG_FILE"
  pip_status=${PIPESTATUS[0]}
  [[ $pip_status -eq 0 ]] || fail "pip 建立失敗。"
fi

if ! "$VENV_PYTHON" -c 'import flask, requests' >/dev/null 2>&1; then
  log "[設定] 正在安裝必要套件，首次執行可能需要一些時間…"
  "$VENV_PYTHON" -m pip install -r "$SCRIPT_DIR/requirements.txt" 2>&1 | tee -a "$LOG_FILE"
  install_status=${PIPESTATUS[0]}
  [[ $install_status -eq 0 ]] \
    || fail "套件安裝失敗。請檢查網路連線及上方 pip 訊息。"
else
  log "[通過] Flask 與 requests 已安裝"
fi

port_is_used() {
  "$VENV_PYTHON" -c "import socket,sys; s=socket.socket(); s.settimeout(0.5); used=s.connect_ex(('127.0.0.1',int(sys.argv[1]))) == 0; s.close(); sys.exit(0 if used else 1)" "$1"
}

APP_PORT=5000
if port_is_used "$APP_PORT"; then
  log "[警告] 連接埠 5000 已被使用（macOS 常見原因是 AirPlay 接收器），改用 5001。"
  APP_PORT=5001
  port_is_used "$APP_PORT" \
    && fail "連接埠 5000 與 5001 都已被使用。請先關閉既有服務，再重新執行。"
fi

log "[通過] 連接埠 $APP_PORT 可以使用"
log "[啟動] 網站網址：http://localhost:$APP_PORT"
log "[提示] 按 Ctrl+C 可停止服務；伺服器輸出會持續寫入本次 LOG。"
printf '\n'

export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
export PORT="$APP_PORT"
"$VENV_PYTHON" "$SCRIPT_DIR/app.py" 2>&1 | tee -a "$LOG_FILE"
server_status=${PIPESTATUS[0]}

if [[ $server_status -eq 0 || $server_status -eq 130 ]]; then
  log "[停止] TrashTrack 服務已停止"
  exit 0
fi

fail "TrashTrack 非正常結束（狀態碼：${server_status}）。"
