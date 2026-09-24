"""Resource snapshot: browser."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.request import Request
from urllib.request import urlopen
import re
from .config import (
    CAPTURE_DEVICE_SCALE_FACTOR,
    DEFAULT_LOCALE,
    DEFAULT_TIMEZONE,
    DEFAULT_VIEWPORT_HEIGHT,
    DEFAULT_VIEWPORT_WIDTH,
    GUEST_USER_AGENT,
    GUEST_VIEWPORT_HEIGHT,
    GUEST_VIEWPORT_WIDTH,
    HLS_DEMOTE_STEPS,
    HLS_MASTER_ROUTE_RE,
    HLS_MAX_AREA_PX,
    HLS_MAX_EDGE_PX,
)


def _prefer_highest_hls_variant(
    body: str,
    *,
    max_edge: int | None = None,
    max_area: int | None = None,
) -> str:
    """Rewrite an HLS master to pin the highest under-cap variant."""
    if "#EXT-X-STREAM-INF" not in body:
        return body

    edge_cap = HLS_MAX_EDGE_PX if max_edge is None else max_edge
    area_cap = HLS_MAX_AREA_PX if max_area is None else max_area

    lines = body.splitlines()
    media_lines: list[str] = []
    preamble: list[str] = []
    streams: list[tuple[int, int, int, str, str, str | None]] = []
    index = 0
    saw_stream = False

    while index < len(lines):
        line = lines[index]
        if line.startswith("#EXT-X-MEDIA:"):
            media_lines.append(line)
            index += 1
            continue
        if line.startswith("#EXT-X-STREAM-INF:"):
            saw_stream = True
            info = line
            uri = lines[index + 1] if index + 1 < len(lines) else ""
            bandwidth = 0
            area = 0
            max_edge_px = 0
            audio_group = None
            match = re.search(r"BANDWIDTH=(\d+)", info)
            if match:
                bandwidth = int(match.group(1))
            match = re.search(r"RESOLUTION=(\d+)x(\d+)", info)
            if match:
                width = int(match.group(1))
                height = int(match.group(2))
                area = width * height
                max_edge_px = max(width, height)
            match = re.search(r'AUDIO="([^"]+)"', info)
            if match:
                audio_group = match.group(1)
            streams.append((area, max_edge_px, bandwidth, info, uri, audio_group))
            index += 2
            continue
        if not saw_stream:
            preamble.append(line)
        index += 1

    if not streams:
        return body

    under_cap = [
        item
        for item in streams
        if item[0] > 0 and item[1] <= edge_cap and item[0] <= area_cap
    ]
    # Prefer the clearest under-cap rung. Keeping multiple variants lets X's
    # ABR settle on 720p even when a 1440p stream is available and decodable.
    candidates = under_cap or streams
    candidates = sorted(
        candidates,
        key=lambda item: (item[0], item[2]),
        reverse=True,
    )
    candidates = candidates[:1]
    audio_groups = {item[5] for item in candidates if item[5]}
    if audio_groups:
        kept_media = [
            line
            for line in media_lines
            if any(f'GROUP-ID="{group}"' in line for group in audio_groups)
        ]
    else:
        kept_media = list(media_lines)

    rewritten: list[str] = [*preamble, *kept_media]
    for _area, _max_edge, _bandwidth, info, uri, _audio_group in candidates:
        rewritten.append(info)
        rewritten.append(uri)
    return "\n".join(rewritten) + "\n"


def _is_hls_media_playlist_url(url: str) -> bool:
    lowered = url.lower()
    return "/avc1/" in lowered or "/mp4a/" in lowered or "/hevc/" in lowered


def _demote_hls_quality_gate(context) -> bool:
    """Lower the HLS soft ceiling after a decode failure. Returns True if lowered."""
    gate = getattr(context, "_hls_quality_gate", None)
    if not isinstance(gate, dict):
        return False
    current_edge = int(gate.get("max_edge") or HLS_MAX_EDGE_PX)
    for edge, area in HLS_DEMOTE_STEPS:
        if current_edge > edge:
            gate["max_edge"] = edge
            gate["max_area"] = area
            return True
    return False


def _tweet_video_undecodable(tweet_card) -> bool:
    """True when a visible tweet video failed to decode / show a frame."""
    try:
        return bool(
            tweet_card.evaluate(
                """(root) => {
                  const video = root.querySelector('video');
                  if (!(video instanceof HTMLVideoElement)) {
                    return false;
                  }
                  const errText = (root.innerText || '').includes('could not be played');
                  if (video.error || errText) {
                    return true;
                  }
                  return video.readyState < 2 || video.videoWidth < 2;
                }"""
            )
        )
    except Exception:
        return False


def _install_high_quality_hls_routes(context) -> None:
    """Rewrite X HLS masters to under-cap multi-variant playlists for ABR."""
    gate = {"max_edge": HLS_MAX_EDGE_PX, "max_area": HLS_MAX_AREA_PX}
    try:
        setattr(context, "_hls_quality_gate", gate)
    except Exception:
        pass

    def handle_route(route) -> None:
        request = route.request
        if _is_hls_media_playlist_url(request.url):
            route.continue_()
            return

        try:
            headers = {
                "User-Agent": request.headers.get("user-agent") or GUEST_USER_AGENT,
                "Accept": "*/*",
                "Referer": "https://x.com/",
                "Origin": "https://x.com",
            }
            accept_language = request.headers.get("accept-language")
            if accept_language:
                headers["Accept-Language"] = accept_language
            fetch_request = Request(request.url, headers=headers)
            with urlopen(fetch_request, timeout=20) as response:
                raw = response.read().decode("utf-8", errors="replace")
                content_type = response.headers.get(
                    "Content-Type",
                    "application/vnd.apple.mpegurl",
                )
            if "#EXT-X-STREAM-INF" not in raw:
                route.continue_()
                return
            body = _prefer_highest_hls_variant(
                raw,
                max_edge=int(gate.get("max_edge") or HLS_MAX_EDGE_PX),
                max_area=int(gate.get("max_area") or HLS_MAX_AREA_PX),
            )
            route.fulfill(
                status=200,
                headers={
                    "content-type": content_type,
                    "access-control-allow-origin": "*",
                    "cache-control": "no-store",
                },
                body=body,
            )
        except Exception:
            try:
                route.continue_()
            except Exception:
                pass

    try:
        context.route(HLS_MASTER_ROUTE_RE, handle_route)
    except Exception:
        pass


@dataclass(frozen=True)
class BrowserSession:
    page: object
    close: Callable[[], None]


def _configure_page(page, *, dark_mode: bool, wait_timeout_ms: int) -> None:
    page.set_default_timeout(wait_timeout_ms)
    page.set_default_navigation_timeout(wait_timeout_ms)
    page.emulate_media(color_scheme="dark" if dark_mode else "light")


def _apply_chinese_locale(context) -> None:
    """Apply Chinese locale overrides for UI formatting and CST timezone.

    NOTE: We intentionally do NOT set the X ``lang`` cookie to zh-cn.
    The lang cookie is what triggers X's server-side auto-translation of
    non-Chinese tweets (ja, ko, …) into Chinese, which replaces the original
    text in the DOM and breaks our translation pipeline.
    All other zh-CN signals (navigator.language, documentElement.lang, Playwright
    locale, Accept-Language) are kept for proper Chinese number/date formatting.
    """
    try:
        context.add_init_script(
            """
            () => {
              Object.defineProperty(navigator, 'language', { get: () => 'zh-CN' });
              Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en-US'] });
              document.documentElement.lang = 'zh-CN';

              // Force Date.getTimezoneOffset to always return UTC+8 (-480 min)
              // so any client-side date formatting uses China Standard Time
              const CST_OFFSET = -480;
              const originalGetTimezoneOffset = Date.prototype.getTimezoneOffset;
              Date.prototype.getTimezoneOffset = function () {
                return CST_OFFSET;
              };

              // Safety net: capture original tweet text as soon as tweetText elements
              // appear in the DOM, before any client-side scripts can modify them.
              const ATTR_ORIG_TEXT = 'data-rs-original-text';
              const ATTR_ORIG_LANG = 'data-rs-original-lang';
              const processed = new WeakSet();

              const looksMostlyChinese = (value) => {
                const text = (value || '').trim();
                if (!text) return false;
                if (/[\u3040-\u30ff\uac00-\ud7af\u1100-\u11ff\u3130-\u318f]/.test(text)) return false;
                const chinese = (text.match(/[\u4e00-\u9fff\u3400-\u4dbf]/g) || []).length;
                const latin = (text.match(/[A-Za-z\u00c0-\u024f]/g) || []).length;
                return chinese > 0 && chinese >= latin;
              };

              const saveOriginal = (el) => {
                const text = (el.innerText || '').trim();
                if (!text) return;
                const existing = (el.getAttribute(ATTR_ORIG_TEXT) || '').trim();
                // Keep a foreign-language snapshot; allow upgrade from Chinese -> source language.
                if (existing && !looksMostlyChinese(existing) && looksMostlyChinese(text)) {
                  processed.add(el);
                  return;
                }
                if (processed.has(el) && existing && !(looksMostlyChinese(existing) && !looksMostlyChinese(text))) {
                  return;
                }
                el.setAttribute(ATTR_ORIG_TEXT, text);
                const lang = el.getAttribute('lang');
                if (lang) el.setAttribute(ATTR_ORIG_LANG, lang);
                processed.add(el);
              };

              const scan = () => {
                document.querySelectorAll('[data-testid="tweetText"]').forEach(saveOriginal);
              };

              new MutationObserver((mutations) => {
                for (const m of mutations) {
                  for (const node of m.addedNodes) {
                    if (node.nodeType === 1) {
                      if (node.matches && node.matches('[data-testid="tweetText"]')) {
                        saveOriginal(node);
                      }
                      if (node.querySelectorAll) {
                        node.querySelectorAll('[data-testid="tweetText"]').forEach(saveOriginal);
                      }
                    }
                  }
                }
              }).observe(document.documentElement, { childList: true, subtree: true });

              if (document.readyState !== 'loading') scan();
              else document.addEventListener('DOMContentLoaded', scan);
            }
            """
        )
    except Exception:
        pass


def _open_capture_session(
    playwright,
    browser_profile: Path,
    *,
    headless: bool,
    dark_mode: bool,
    wait_timeout_ms: int,
    guest_mode: bool,
) -> BrowserSession:
    if guest_mode:
        browser = playwright.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            viewport={"width": GUEST_VIEWPORT_WIDTH, "height": GUEST_VIEWPORT_HEIGHT},
            device_scale_factor=CAPTURE_DEVICE_SCALE_FACTOR,
            color_scheme="dark" if dark_mode else "light",
            user_agent=GUEST_USER_AGENT,
            locale=DEFAULT_LOCALE,
            timezone_id=DEFAULT_TIMEZONE,
            extra_http_headers={
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )
        _apply_chinese_locale(context)
        _install_high_quality_hls_routes(context)
        page = context.new_page()
        _configure_page(page, dark_mode=dark_mode, wait_timeout_ms=wait_timeout_ms)

        def close() -> None:
            context.close()
            browser.close()

        return BrowserSession(page=page, close=close)

    context = playwright.chromium.launch_persistent_context(
        str(browser_profile),
        headless=headless,
        viewport={"width": DEFAULT_VIEWPORT_WIDTH, "height": DEFAULT_VIEWPORT_HEIGHT},
        device_scale_factor=CAPTURE_DEVICE_SCALE_FACTOR,
        locale=DEFAULT_LOCALE,
        timezone_id=DEFAULT_TIMEZONE,
        color_scheme="dark" if dark_mode else "light",
        ignore_https_errors=True,
        args=["--disable-blink-features=AutomationControlled"],
        extra_http_headers={
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )
    _apply_chinese_locale(context)
    _install_high_quality_hls_routes(context)
    page = context.pages[0] if context.pages else context.new_page()
    _configure_page(page, dark_mode=dark_mode, wait_timeout_ms=wait_timeout_ms)

    def close() -> None:
        context.close()

    return BrowserSession(page=page, close=close)
