#!/usr/bin/env python3
"""Local web app for capturing X/Twitter detail-page screenshots."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import threading
import webbrowser

from flask import Flask, jsonify, request, send_from_directory

from screenshot_service import capture_tweet_page


ROOT = Path(__file__).resolve().parent
SCREENSHOTS_DIR = ROOT / "screenshots"
PROFILE_DIR = ROOT / "browser_profile"
PORT = 5080

app = Flask(__name__, static_folder="static")

_RUN_LOCK = threading.Lock()
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tweet-shot")


@app.get("/")
def index():
    return send_from_directory(ROOT / "static", "index.html")


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.get("/screenshots/<path:filename>")
def screenshots(filename: str):
    return send_from_directory(SCREENSHOTS_DIR, filename)


@app.post("/api/capture")
def api_capture():
    payload = request.get_json(silent=True) or {}
    url = (payload.get("url") or "").strip()
    show_browser = bool(payload.get("showBrowser"))
    dark_mode = payload.get("darkMode")
    dark_mode = True if dark_mode is None else bool(dark_mode)

    if not url:
        return jsonify({"ok": False, "error": "请输入推文链接"}), 400

    if not _RUN_LOCK.acquire(blocking=False):
        return jsonify({"ok": False, "error": "已有截图任务在运行，请稍后再试"}), 429

    try:
        result = _EXECUTOR.submit(
            capture_tweet_page,
            url,
            SCREENSHOTS_DIR,
            PROFILE_DIR,
            headless=not show_browser,
            dark_mode=dark_mode,
        ).result()
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        _RUN_LOCK.release()

    return jsonify(
        {
            "ok": True,
            "message": "截图已保存",
            "fileName": result.file_name,
            "savedTo": str(result.file_path),
            "previewUrl": result.preview_url,
            "captureMode": result.capture_mode,
            "usedUrl": result.used_url,
            "tweetId": result.tweet_id,
        }
    )


def _open_browser() -> None:
    webbrowser.open(f"http://127.0.0.1:{PORT}")


if __name__ == "__main__":
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    if os.getenv("NO_AUTO_OPEN") != "1":
        timer = threading.Timer(0.8, _open_browser)
        timer.daemon = True
        timer.start()

    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=False, use_reloader=False)
