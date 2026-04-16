#!/bin/zsh
cd "$(dirname "$0")"

if python -c "import flask, playwright" >/dev/null 2>&1; then
  PYTHON_BIN="python"
elif python3 -c "import flask, playwright" >/dev/null 2>&1; then
  PYTHON_BIN="python3"
else
  echo "没有找到已安装 Flask 和 Playwright 的 Python 环境。"
  echo "请先运行："
  echo "  python -m pip install -r requirements.txt"
  echo "  playwright install chromium"
  read -k 1 "?按任意键退出..."
  echo
  exit 1
fi

APP_URL="http://127.0.0.1:5080"

service_is_running() {
  if command -v curl >/dev/null 2>&1; then
    local response
    response="$(curl --silent --show-error --max-time 2 "${APP_URL}/health" 2>/dev/null)" || return 1
    [[ "$response" == *'"ok"'* ]]
    return $?
  fi

  "$PYTHON_BIN" - "${APP_URL}/health" >/dev/null 2>&1 <<'PY'
import json
import sys
import urllib.request

url = sys.argv[1]
with urllib.request.urlopen(url, timeout=2) as response:
    payload = json.load(response)

raise SystemExit(0 if payload.get("ok") is True else 1)
PY
}

if service_is_running; then
  echo "检测到服务已在运行，无需重复启动。"
  echo "直接访问: ${APP_URL}"

  if [[ "${NO_AUTO_OPEN:-0}" != "1" ]]; then
    "$PYTHON_BIN" -c "import webbrowser; webbrowser.open('${APP_URL}')"
  fi
  exit 0
fi

"$PYTHON_BIN" app.py
