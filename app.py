#!/usr/bin/env python3
"""Local web app for capturing X/Twitter detail-page screenshots."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import threading
import webbrowser

from flask import Flask, jsonify, request, send_from_directory

from screenshot_service import (
    capture_tweet_page,
    parse_video_timestamps_from_request,
    preview_tweet_translations,
)


ROOT = Path(__file__).resolve().parent
SCREENSHOTS_DIR = ROOT / "screenshots"
PROFILE_DIR = ROOT / "browser_profile"
PORT = 5080

app = Flask(__name__, static_folder="static")

_RUN_LOCK = threading.Lock()
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tweet-shot")


@app.get("/")
def index():
    response = send_from_directory(ROOT / "static", "index.html")
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


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
    video_time = payload.get("videoTime")
    video_times = payload.get("videoTimes")
    custom_translation = (payload.get("customTranslation") or "").strip()
    translation_overrides_payload = payload.get("translationOverrides") or []
    show_browser = bool(payload.get("showBrowser"))
    dark_mode = payload.get("darkMode")
    translate_body = payload.get("translateBody")
    dark_mode = True if dark_mode is None else bool(dark_mode)
    translate_body = False if translate_body is None else bool(translate_body)
    translation_overrides: dict[int, str] = {}

    if isinstance(translation_overrides_payload, list):
        for item in translation_overrides_payload:
            if not isinstance(item, dict):
                continue
            try:
                index = int(item.get("index"))
            except (TypeError, ValueError):
                continue
            translation_overrides[index] = str(item.get("translation") or "")

    if not url:
        return jsonify({"ok": False, "error": "请输入推文链接"}), 400

    try:
        video_frame_schedule = parse_video_timestamps_from_request(video_time, video_times)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

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
            video_frame_schedule=video_frame_schedule,
            translate_body=translate_body,
            custom_translation=custom_translation,
            translation_overrides=translation_overrides,
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
            "videoFrameSeconds": result.video_frame_seconds,
            "videoFrames": [
                {
                    "index": frame.index,
                    "label": frame.label,
                    "seconds": frame.seconds,
                    "quoted": frame.quoted,
                }
                for frame in result.video_frames
            ],
        }
    )


@app.post("/api/preview-translations")
def api_preview_translations():
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
            preview_tweet_translations,
            url,
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
            "items": [
                {
                    "index": item.index,
                    "label": item.label,
                    "originalText": item.original_text,
                    "translation": item.suggested_translation,
                }
                for item in result.items
            ],
            "usedUrl": result.used_url,
            "captureMode": result.capture_mode,
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
