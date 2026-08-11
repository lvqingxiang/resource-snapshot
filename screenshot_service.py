#!/usr/bin/env python3
"""Capture a screenshot for a single X/Twitter status URL."""

from __future__ import annotations

from dataclasses import dataclass
from html import unescape
import json
from pathlib import Path
import re
import time
from typing import Callable
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


TWEET_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:x\.com|twitter\.com|mobile\.twitter\.com)/"
    r"(?P<screen_name>[^/?#]+)/status/(?P<tweet_id>\d+)",
    re.IGNORECASE,
)
GOOGLE_TRANSLATE_API_URL = "https://translate.googleapis.com/translate_a/single"
MYMEMORY_TRANSLATE_API_URL = "https://api.mymemory.translated.net/get"
TWITTER_OEMBED_API_URL = "https://publish.twitter.com/oembed"
TRANSLATION_ATTR = "data-resource-snapshot-translation"
DEFAULT_VIEWPORT_WIDTH = 1280
DEFAULT_VIEWPORT_HEIGHT = 1800
CAPTURE_VIEWPORT_MARGIN = 32
CAPTURE_DEVICE_SCALE_FACTOR = 2
GUEST_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
GUEST_VIEWPORT_WIDTH = 1280
GUEST_VIEWPORT_HEIGHT = 2000
GUEST_PAGE_SETTLE_MS = 8000
GUEST_POST_SCROLL_MS = 3000
DEFAULT_LOCALE = "zh-CN"
DEFAULT_TIMEZONE = "Asia/Shanghai"
HLS_MASTER_ROUTE_RE = re.compile(
    r"https://video\.twimg\.com/.*\.m3u8(?:\?.*)?$",
    re.IGNORECASE,
)
# Soft ceiling for headless Chromium: strip 4K / absurdly large amplify
# streams (e.g. 2160x3840) that often fail with MEDIA_ELEMENT_ERROR.
# Keep multiple ~1080p-and-below variants (not a single pinned URI) so
# clearer frames are preferred when decodable. X's player may still hard-
# fail on some 1080p profiles without ABR fallback — see demote steps.
HLS_MAX_EDGE_PX = 1920
HLS_MAX_AREA_PX = 1920 * 1080
# Progressive ceilings used when the current playlist still won't decode.
HLS_DEMOTE_STEPS: tuple[tuple[int, int], ...] = (
    (1280, 1280 * 720),
    (854, 854 * 480),
    (640, 640 * 360),
)


def _prefer_highest_hls_variant(
    body: str,
    *,
    max_edge: int | None = None,
    max_area: int | None = None,
) -> str:
    """Rewrite an HLS master to keep multiple under-cap variants for ABR.

    Drops streams above the soft ceiling while retaining several rungs
    (sorted highest-first) so clearer frames are preferred when present.
    Does not pin a single URI — lower rungs remain for player fallback.
    """
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
    # Prefer under-cap rungs; if none qualify, keep originals unchanged
    # rather than inventing a single pinned URI.
    candidates = under_cap or streams
    candidates = sorted(
        candidates,
        key=lambda item: (item[0], item[2]),
        reverse=True,
    )
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


def _translation_capture_css(dark_mode: bool) -> str:
    background = "rgba(29, 155, 240, 0.12)" if dark_mode else "rgba(29, 155, 240, 0.10)"
    text = "#e7e9ea" if dark_mode else "#0f1419"
    muted = "#8ecdfd" if dark_mode else "#1d6fa5"
    border = "#1d9bf0"

    return f"""
[{TRANSLATION_ATTR}="block"] {{
  margin-top: 10px !important;
  padding: 10px 12px !important;
  border-left: 3px solid {border} !important;
  border-radius: 14px !important;
  background: {background} !important;
}}

[{TRANSLATION_ATTR}="label"] {{
  display: block !important;
  margin-bottom: 4px !important;
  color: {muted} !important;
  font-size: 13px !important;
  line-height: 1.4 !important;
  letter-spacing: 0.02em !important;
}}

[{TRANSLATION_ATTR}="body"] {{
  display: block !important;
  color: {text} !important;
  font-size: 15px !important;
  line-height: 1.65 !important;
  white-space: pre-wrap !important;
  word-break: break-word !important;
}}
"""


def _detail_capture_css(dark_mode: bool) -> str:
    background = "#000000" if dark_mode else "#ffffff"
    text = "#e7e9ea" if dark_mode else "#0f1419"
    muted = "#71767b" if dark_mode else "#536471"
    border = "#2f3336" if dark_mode else "#eff3f4"
    link = "#1d9bf0"

    return f"""
html {{
  scroll-behavior: auto !important;
  background: {background} !important;
  color-scheme: {"dark" if dark_mode else "light"} !important;
}}

body {{
  background: {background} !important;
  color: {text} !important;
}}

[data-testid="BottomBar"],
[data-testid="DMDrawer"],
[data-testid="sidebarColumn"],
header[role="banner"],
[data-testid="logged_out_read_replies_pivot"],
[data-testid="inline_reply_offscreen"],
[data-testid="tweet-text-show-more-link"] {{
  display: none !important;
}}

[data-testid="tweetText"],
article div[dir="auto"] {{
  overflow: visible !important;
  max-height: none !important;
  -webkit-line-clamp: unset !important;
  line-clamp: unset !important;
  word-break: keep-all !important;
  overflow-wrap: anywhere !important;
  white-space: pre-wrap !important;
}}

article [data-testid="videoComponent"],
article [data-testid="videoPlayer"] {{
  width: 100% !important;
  max-width: 100% !important;
  margin-left: auto !important;
  margin-right: auto !important;
  background: transparent !important;
  min-width: 0 !important;
}}

article [data-testid="tweetPhoto"] {{
  max-width: 100% !important;
  background: transparent !important;
  min-width: 0 !important;
  overflow: hidden !important;
}}

/* Single-photo posts: show the full image without expanding flex min-content.
   Multi-photo grids keep X's cover crop via the rules below / JS prep. */
article [data-testid="tweetPhoto"] img {{
  display: block !important;
  max-width: 100% !important;
  background: transparent !important;
}}

article[data-resource-snapshot-single-photo] [data-testid="tweetPhoto"] {{
  width: 100% !important;
  margin-left: auto !important;
  margin-right: auto !important;
}}

article[data-resource-snapshot-single-photo] [data-testid="tweetPhoto"] img {{
  width: 100% !important;
  height: auto !important;
  object-fit: contain !important;
}}

article[data-resource-snapshot-multi-photo] [data-testid="tweetPhoto"] img {{
  width: 100% !important;
  height: 100% !important;
  object-fit: cover !important;
  object-position: center center !important;
}}

article video {{
  display: block !important;
  max-width: 100% !important;
  background: transparent !important;
}}

article video::-webkit-media-controls,
article video::-webkit-media-controls-enclosure,
article video::-webkit-media-controls-panel {{
  display: none !important;
}}

article [data-testid="videoComponent"] [role="progressbar"],
article [data-testid="videoComponent"] [role="slider"],
article [data-testid="videoComponent"] input[type="range"],
article [data-testid="videoPlayer"] [role="progressbar"],
article [data-testid="videoPlayer"] [role="slider"],
article [data-testid="videoPlayer"] input[type="range"] {{
  display: none !important;
}}

[data-resource-snapshot-media-grid] {{
  width: 100% !important;
  border-radius: 16px !important;
  overflow: hidden !important;
  background: {border} !important;
}}

[data-resource-snapshot-media-grid] img {{
  width: 100% !important;
  height: 100% !important;
  object-fit: cover !important;
  display: block !important;
}}

main article,
article[data-testid="tweet"],
article[data-tweet-id] {{
  width: 100% !important;
  max-width: min(598px, calc(100vw - 32px)) !important;
  margin-left: auto !important;
  margin-right: auto !important;
  overflow: hidden !important;
}}

main[role="main"] {{
  display: block !important;
  background: {background} !important;
  overflow: hidden !important;
}}

[data-testid="primaryColumn"] {{
  width: min(760px, calc(100vw - 32px)) !important;
  max-width: none !important;
  margin: 0 auto !important;
  background: {background} !important;
  overflow: hidden !important;
}}

article[data-testid="tweet"],
article[data-tweet-id],
[data-testid="cellInnerDiv"],
[data-testid="tweet"],
[data-testid="tweetText"],
[data-testid="tweetPhoto"],
[role="group"] {{
  background: {background} !important;
  border-color: {border} !important;
}}

article[data-testid="tweet"],
article[data-testid="tweet"] * {{
  color: {text} !important;
}}

article[data-testid="tweet"] a,
article[data-testid="tweet"] a * {{
  color: {link} !important;
}}

article[data-testid="tweet"] time,
article[data-testid="tweet"] time *,
[data-testid="User-Name"] span:last-child,
[data-testid="app-text-transition-container"] {{
  color: {muted} !important;
}}
{_translation_capture_css(dark_mode)}
"""


@dataclass(frozen=True)
class VideoFrameInfo:
    index: int
    label: str
    seconds: float
    quoted: bool


@dataclass(frozen=True)
class CaptureResult:
    file_name: str
    file_path: Path
    preview_url: str
    capture_mode: str
    used_url: str
    tweet_id: str
    video_frame_seconds: float | None
    video_frames: tuple[VideoFrameInfo, ...] = ()


@dataclass(frozen=True)
class TranslationPreviewItem:
    index: int
    label: str
    original_text: str
    suggested_translation: str


@dataclass(frozen=True)
class TranslationPreviewResult:
    items: tuple[TranslationPreviewItem, ...]
    used_url: str
    capture_mode: str
    tweet_id: str


def _normalize_input_url(url: str) -> str:
    value = (url or "").strip()
    if not value:
        raise ValueError("请输入 X/Twitter 推文链接")
    if not value.startswith(("http://", "https://")):
        value = f"https://{value}"

    parsed = urlparse(value)
    cleaned = parsed._replace(query="", fragment="")
    normalized = cleaned.geturl()

    match = TWEET_URL_RE.match(normalized)
    if not match:
        raise ValueError("只支持单条推文链接，格式例如 https://x.com/.../status/1234567890")

    host = parsed.netloc.lower()
    if host not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}:
        raise ValueError("请输入有效的 X/Twitter 推文详情页链接")

    return normalized


def _extract_parts(url: str) -> tuple[str, str]:
    match = TWEET_URL_RE.match(url)
    if not match:
        raise ValueError("无法识别推文 ID")
    return match.group("screen_name"), match.group("tweet_id")


def _extract_status_id_from_url(url: str | None) -> str | None:
    match = re.search(
        r"/(?:i/(?:web/)?status|[^/?#]+/status)/(?P<tweet_id>\d+)",
        url or "",
        flags=re.IGNORECASE,
    )
    return match.group("tweet_id") if match else None


def _candidate_urls(original_url: str, screen_name: str, tweet_id: str) -> list[tuple[str, str]]:
    parsed = urlparse(original_url)
    original_host = parsed.netloc.lower() or "x.com"

    detail_path = f"/{screen_name}/status/{tweet_id}"
    urls = [
        (f"https://{original_host}{detail_path}", "detail_page"),
        (f"https://x.com/i/status/{tweet_id}", "detail_page"),
        (f"https://twitter.com/i/status/{tweet_id}", "detail_page"),
    ]

    unique: list[tuple[str, str]] = []
    seen: set[str] = set()
    for candidate_url, mode in urls:
        if candidate_url in seen:
            continue
        seen.add(candidate_url)
        unique.append((candidate_url, mode))
    return unique


def _dismiss_common_overlays(page) -> None:
    for _ in range(2):
        try:
            page.keyboard.press("Escape")
        except Exception:
            break

    page.evaluate(
        """
        () => {
          const isVisible = (node) => {
            if (!(node instanceof HTMLElement)) {
              return false;
            }
            const style = window.getComputedStyle(node);
            if (!style) {
              return false;
            }
            if (style.display === 'none' || style.visibility === 'hidden') {
              return false;
            }
            const rect = node.getBoundingClientRect();
            return rect.width > 20 && rect.height > 20;
          };

          const selectors = [
            '[role="dialog"]',
            '[data-testid="sheetDialog"]',
            '[data-testid="BottomBar"]',
            '[data-testid="DMDrawer"]'
          ];
          for (const selector of selectors) {
            document.querySelectorAll(selector).forEach((node) => node.remove());
          }

          const primaryColumn = document.querySelector('[data-testid="primaryColumn"]');
          if (primaryColumn instanceof HTMLElement) {
            for (const node of primaryColumn.querySelectorAll('*')) {
              if (!(node instanceof HTMLElement) || !isVisible(node)) {
                continue;
              }

              const style = window.getComputedStyle(node);
              const rect = node.getBoundingClientRect();
              const text = (node.innerText || '').trim();

              const isTopStickyBar =
                (style.position === 'sticky' || style.position === 'fixed') &&
                rect.top <= 1 &&
                rect.height <= 80;

              const isTopFeedNotice =
                text &&
                ['查看新帖子', 'View new posts', 'See new posts'].includes(text) &&
                rect.top < 140 &&
                rect.height <= 80;

              if (isTopStickyBar || isTopFeedNotice) {
                node.remove();
              }
            }
          }

          document.documentElement.style.scrollBehavior = 'auto';
          document.body.style.overflow = 'auto';
        }
        """
    )


def _expand_tweet_text(tweet_card) -> None:
    try:
        tweet_card.evaluate(
            """
            (root) => {
              const clickables = [
                ...root.querySelectorAll('[data-testid="tweet-text-show-more-link"]'),
                ...root.querySelectorAll('a, button, [role="button"], span'),
              ];
              for (const node of clickables) {
                const text = (node.textContent || '').trim();
                if (!text || text.length > 24) {
                  continue;
                }
                if (/show more|显示更多|展开全文|顯示更多|もっと見る|さらに表示/i.test(text)) {
                  node.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                }
              }

              for (const node of root.querySelectorAll('[data-testid="tweetText"], div[dir="auto"]')) {
                if (!(node instanceof HTMLElement)) {
                  continue;
                }
                node.style.setProperty('overflow', 'visible', 'important');
                node.style.setProperty('max-height', 'none', 'important');
                node.style.setProperty('-webkit-line-clamp', 'unset', 'important');
                node.style.setProperty('line-clamp', 'unset', 'important');
                node.style.setProperty('display', 'block', 'important');
              }
            }
            """
        )
    except Exception:
        pass


def _hide_non_primary_columns(page, tweet_id: str | None = None) -> None:
    try:
        page.evaluate(
            """
            (tweetId) => {
              const removeNode = (node) => {
                if (node instanceof Element) {
                  node.remove();
                }
              };

              const hideNode = (node) => {
                if (node instanceof HTMLElement) {
                  node.style.setProperty('display', 'none', 'important');
                  node.style.setProperty('visibility', 'hidden', 'important');
                  node.style.setProperty('pointer-events', 'none', 'important');
                  node.style.setProperty('height', '0', 'important');
                  node.style.setProperty('max-height', '0', 'important');
                  node.style.setProperty('overflow', 'hidden', 'important');
                  node.style.setProperty('margin', '0', 'important');
                  node.style.setProperty('padding', '0', 'important');
                }
              };

              const articleMatchesTweet = (article) => {
                if (!(article instanceof Element)) {
                  return false;
                }
                if (!tweetId) {
                  return true;
                }
                if (article.getAttribute('data-tweet-id') === tweetId) {
                  return true;
                }
                if (article.querySelector(`a[href*="/status/${tweetId}"]`)) {
                  return true;
                }
                return false;
              };

              const selectors = [
                '[data-testid="sidebarColumn"]',
                '[data-testid="secondaryColumn"]',
                '[data-testid="BottomBar"]',
                '[data-testid="DMDrawer"]',
                'header[role="banner"]',
              ];
              for (const selector of selectors) {
                document.querySelectorAll(selector).forEach(removeNode);
              }

              const primary = document.querySelector('[data-testid="primaryColumn"]');
              if (primary?.parentElement) {
                for (const child of [...primary.parentElement.children]) {
                  if (child !== primary) {
                    removeNode(child);
                  }
                }
              }

              const main = document.querySelector('main[role="main"]');
              if (main) {
                const children = [...main.children].filter((node) => node instanceof HTMLElement);
                if (children.length > 1) {
                  const primaryChild =
                    children.find((node) => node.querySelector('article[data-tweet-id], article[data-testid="tweet"]')) ||
                    children[0];
                  for (const child of children) {
                    if (child !== primaryChild) {
                      removeNode(child);
                    }
                  }
                }
              }

              // Remove / collapse every non-target tweet cell so replies cannot peek
              // into the screenshot after viewport resizes reflow the timeline.
              const articles = [...document.querySelectorAll(
                'article[data-tweet-id], article[data-testid="tweet"]'
              )];
              let targetArticle =
                articles.find((article) => articleMatchesTweet(article)) || articles[0] || null;

              for (const article of articles) {
                if (article === targetArticle) {
                  continue;
                }
                if (targetArticle?.contains(article)) {
                  continue;
                }
                const cell = article.closest('[data-testid="cellInnerDiv"]') || article;
                removeNode(cell);
              }

              if (targetArticle) {
                const targetCell = targetArticle.closest('[data-testid="cellInnerDiv"]');
                if (targetCell?.parentElement) {
                  for (const sibling of [...targetCell.parentElement.children]) {
                    if (sibling !== targetCell && sibling instanceof HTMLElement) {
                      // Keep structural spacers out of the capture by removing them.
                      if (
                        sibling.querySelector(
                          'article[data-tweet-id], article[data-testid="tweet"], [data-testid="User-Name"]',
                        ) ||
                        (sibling.textContent || '').trim().length > 0
                      ) {
                        removeNode(sibling);
                      } else {
                        hideNode(sibling);
                      }
                    }
                  }
                }

                // Also collapse siblings of any ancestor cell wrapper
                let current = targetCell;
                while (current?.parentElement) {
                  const parent = current.parentElement;
                  if (parent === main || parent === primary || parent === document.body) break;
                  for (const sibling of [...parent.children]) {
                    if (sibling !== current && sibling instanceof HTMLElement) {
                      if (
                        sibling.querySelector(
                          'article[data-tweet-id], article[data-testid="tweet"], [data-testid="User-Name"]',
                        )
                      ) {
                        removeNode(sibling);
                      } else {
                        hideNode(sibling);
                      }
                    }
                  }
                  current = parent;
                }
              }

              // Hide any remaining peek / suggestion / composer elements
              const peekSelectors = [
                '[data-testid="tweet-detail-more-replies"]',
                '[data-testid="conversation-more-replies"]',
                '[data-testid="related-tweets"]',
                '[data-testid="inline_reply_offscreen"]',
                '[data-testid="logged_out_read_replies_pivot"]',
                '[data-testid="inline_reply_composer"]',
                '[data-testid="tweetTextarea_0"]',
                '[data-testid="tweetButtonInline"]',
                'form[aria-label*="Reply"]',
                'form[aria-label*="reply"]',
                'form[aria-label*="回复"]',
                'form[aria-label*="回覆"]',
                'output',
              ];
              for (const sel of peekSelectors) {
                document.querySelectorAll(sel).forEach(removeNode);
              }

              // Drop every timeline cell after the target tweet (reply composer
              // skeletons, "only some accounts can reply", next tweets, etc.).
              if (targetArticle) {
                const targetCell =
                  targetArticle.closest('[data-testid="cellInnerDiv"]') || targetArticle;
                let sibling = targetCell.nextElementSibling;
                while (sibling) {
                  const next = sibling.nextElementSibling;
                  removeNode(sibling);
                  sibling = next;
                }
                // Also clear later siblings of ancestor section wrappers.
                let current = targetCell.parentElement;
                while (current && current !== main && current !== primary && current !== document.body) {
                  sibling = current.nextElementSibling;
                  while (sibling) {
                    const next = sibling.nextElementSibling;
                    removeNode(sibling);
                    sibling = next;
                  }
                  current = current.parentElement;
                }
              }
            }
            """,
            tweet_id,
        )
    except Exception:
        pass


def _tweet_card_matches_id(tweet_card, tweet_id: str) -> bool:
    try:
        return bool(
            tweet_card.evaluate(
                """
                (el, tweetId) => {
                  if (el.getAttribute('data-tweet-id') === tweetId) {
                    return true;
                  }
                  if (el.querySelector(`article[data-tweet-id="${tweetId}"]`)) {
                    return true;
                  }
                  if (el.querySelector(`a[href*="/status/${tweetId}"]`)) {
                    return true;
                  }
                  return false;
                }
                """,
                tweet_id,
            )
        )
    except Exception:
        return False


def _wait_for_tweet_card(page, tweet_id: str, timeout_ms: int):
    permalink = page.locator(
        ",".join(
            [
                f"a[href*='/status/{tweet_id}']",
                f"a[href*='/i/web/status/{tweet_id}']",
                f"a[href$='/{tweet_id}']",
            ]
        )
    )
    locators = [
        page.locator(f'article[data-tweet-id="{tweet_id}"]').first,
        page.locator("article").filter(has=permalink).first,
        page.locator("article[data-testid='tweet']").filter(has=permalink).first,
        page.locator("[data-testid='cellInnerDiv']").filter(has=permalink).locator("article").first,
        page.locator("main article").filter(has=permalink).first,
        page.locator("main article").first,
    ]

    deadline = time.monotonic() + (timeout_ms / 1000)
    per_locator_ms = max(2500, timeout_ms // max(len(locators), 1))

    for locator in locators:
        remaining_ms = int(max(0, (deadline - time.monotonic()) * 1000))
        if remaining_ms <= 0:
            break
        slot_ms = min(per_locator_ms, remaining_ms)
        try:
            locator.wait_for(state="visible", timeout=slot_ms)
            if _tweet_card_matches_id(locator, tweet_id):
                return locator
        except PlaywrightTimeoutError:
            continue
    return None


def _scroll_tweet_into_view(page, tweet_card, *, guest_mode: bool = False) -> None:
    tweet_card.scroll_into_view_if_needed(timeout=10000)
    page.wait_for_timeout(GUEST_POST_SCROLL_MS if guest_mode else 500)

    box = tweet_card.bounding_box()
    if not box:
        return

    top_padding = 16 if guest_mode else 120
    target_top = max(int(box["y"] - top_padding), 0)
    page.evaluate("(top) => window.scrollTo(0, top)", target_top)
    page.wait_for_timeout(300 if guest_mode else 700)


def _wait_for_tweet_assets(page, tweet_card) -> None:
    element = tweet_card.element_handle(timeout=5000)
    if element is None:
        return

    try:
        page.wait_for_function(
            """
            (el) => {
              const images = [...el.querySelectorAll('img')]
                .filter((img) => img.offsetParent !== null);
              return images.length === 0 || images.every(
                (img) => img.complete && img.naturalWidth > 0
              );
            }
            """,
            arg=element,
            timeout=8000,
        )
    except PlaywrightTimeoutError:
        pass

    try:
        page.wait_for_function(
            """
            (el) => {
              const busyNodes = [...el.querySelectorAll('[aria-busy="true"], [role="progressbar"]')]
                .filter((node) => node.offsetParent !== null);
              return busyNodes.length === 0;
            }
            """,
            arg=element,
            timeout=4000,
        )
    except PlaywrightTimeoutError:
        pass

    try:
        page.wait_for_function(
            """
            (el) => {
              const videos = [...el.querySelectorAll('video')].filter((video) => {
                const style = window.getComputedStyle(video);
                if (!style) {
                  return false;
                }
                if (style.display === 'none' || style.visibility === 'hidden') {
                  return false;
                }
                const rect = video.getBoundingClientRect();
                return rect.width >= 48 && rect.height >= 48;
              });
              return videos.length === 0 || videos.every((video) => video.readyState >= 1);
            }
            """,
            arg=element,
            timeout=6000,
        )
    except PlaywrightTimeoutError:
        pass

    page.wait_for_timeout(1200)


# Regex: Hiragana, Katakana, Hangul (Japanese/Korean scripts)
_NON_CHINESE_SCRIPT_RE = re.compile(
    "[\u3040-\u309F"  # Hiragana
    "\u30A0-\u30FF"   # Katakana
    "\uAC00-\uD7AF"   # Hangul Syllables
    "\u1100-\u11FF"   # Hangul Jamo
    "\u3130-\u318F"   # Hangul Compatibility Jamo
    "]"
)
_CHINESE_CHAR_RE = re.compile("[\u4e00-\u9fff\u3400-\u4dbf]")
# Latin letters including common Western European accents (Spanish, French, etc.)
_LATIN_LETTER_RE = re.compile("[A-Za-z\u00c0-\u024f\u1e00-\u1eff]")


def _text_looks_non_chinese(text: str) -> bool:
    """True when text still looks like a foreign-language source, not Chinese.

    X may mark auto-translated (or restored) tweet nodes as lang=zh even when the
    visible body is Spanish/English/Japanese/etc. Only treat lang=zh as "skip
    translation" when the text itself is predominantly Chinese.
    """
    normalized = (text or "").strip()
    if not normalized:
        return False
    if _NON_CHINESE_SCRIPT_RE.search(normalized):
        return True

    chinese_count = len(_CHINESE_CHAR_RE.findall(normalized))
    latin_count = len(_LATIN_LETTER_RE.findall(normalized))
    # Enough Latin letters and not dominated by Chinese → needs translation.
    return latin_count >= 4 and latin_count > chinese_count


def _text_looks_mostly_chinese(text: str) -> bool:
    normalized = (text or "").strip()
    if not normalized or _NON_CHINESE_SCRIPT_RE.search(normalized):
        return False
    chinese_count = len(_CHINESE_CHAR_RE.findall(normalized))
    latin_count = len(_LATIN_LETTER_RE.findall(normalized))
    return chinese_count > 0 and chinese_count >= latin_count


_LOOKS_MOSTLY_CHINESE_JS = r"""
(value) => {
  const text = (value || '').trim();
  if (!text) {
    return false;
  }
  if (/[\u3040-\u30ff\uac00-\ud7af\u1100-\u11ff\u3130-\u318f]/.test(text)) {
    return false;
  }
  const chinese = (text.match(/[\u4e00-\u9fff\u3400-\u4dbf]/g) || []).length;
  const latin = (text.match(/[A-Za-z\u00c0-\u024f]/g) || []).length;
  return chinese > 0 && chinese >= latin;
}
"""


def _normalize_translation_lang(lang: str | None) -> str | None:
    value = (lang or "").strip()
    if not value:
        return None

    lowered = value.replace("_", "-").lower()
    if lowered in {"zh", "zh-cn", "zh-hans", "zh-sg"}:
        return "zh-CN"
    if lowered in {"zh-tw", "zh-hk", "zh-hant"}:
        return "zh-TW"
    return lowered


def _detect_translation_source_lang(text: str) -> str | None:
    """Infer a source language when the tweet DOM does not expose lang=."""
    sample = (text or "").strip()
    if not sample:
        return None

    if re.search(r"[\u3040-\u30ff]", sample):
        return "ja"
    if re.search(r"[\uac00-\ud7af]", sample):
        return "ko"
    if re.search(r"[\u0400-\u04ff]", sample):
        return "ru"
    if re.search(r"[A-Za-z]", sample) and not re.search(r"[\u4e00-\u9fff]", sample):
        return "en"
    return None


def _looks_like_chinese_text(text: str) -> bool:
    sample = (text or "").strip()
    if not sample:
        return False
    if re.search(r"[\u3040-\u30ff\uac00-\ud7af]", sample):
        return False
    han = len(re.findall(r"[\u4e00-\u9fff]", sample))
    latin = len(re.findall(r"[A-Za-z]", sample))
    return han > 0 and han >= latin


def _fetch_translation_payload(url: str) -> object | None:
    request = Request(
        url,
        headers={
            "User-Agent": "resource-snapshot/1.0",
            "Accept": "application/json",
        },
    )

    try:
        with urlopen(request, timeout=12) as response:
            return json.load(response)
    except Exception:
        return None


def _fetch_oembed_tweet_body(status_url: str) -> tuple[str | None, str | None]:
    """Return (original_text, lang) from Twitter oEmbed, bypassing X page auto-translate."""
    normalized = (status_url or "").strip()
    if not normalized:
        return None, None

    query = urlencode({"url": normalized, "omit_script": "1"})
    payload = _fetch_translation_payload(f"{TWITTER_OEMBED_API_URL}?{query}")
    if not isinstance(payload, dict):
        return None, None

    html = str(payload.get("html") or "")
    match = re.search(
        r'<p\b([^>]*)>(.*?)</p>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None, None

    attr_blob = match.group(1) or ""
    lang_match = re.search(r'\blang=["\']([^"\']+)["\']', attr_blob, flags=re.IGNORECASE)
    lang = _normalize_translation_lang(lang_match.group(1) if lang_match else None)

    inner = match.group(2) or ""
    text = unescape(re.sub(r"<[^>]+>", "", inner))
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s*https?://t\.co/\w+\s*$", "", text).strip()
    text = re.sub(r"\s*pic\.twitter\.com/\w+\s*$", "", text, flags=re.IGNORECASE).strip()
    if not text:
        return None, None
    return text, lang


def _seed_original_text_from_oembed(tweet_card, original_text: str, lang: str | None) -> None:
    """Attach oEmbed original text and restore visible body when X left a Chinese translation."""
    try:
        tweet_card.evaluate(
            f"""
            (root, payload) => {{
              const looksMostlyChinese = {_LOOKS_MOSTLY_CHINESE_JS};
              const text = (payload.text || '').trim();
              const lang = (payload.lang || '').trim();
              if (!text) {{
                return;
              }}
              for (const el of root.querySelectorAll('[data-testid="tweetText"]')) {{
                const live = (el.innerText || '').trim();
                const existing = (el.getAttribute('data-rs-original-text') || '').trim();
                if (!existing || (looksMostlyChinese(existing) && !looksMostlyChinese(text))) {{
                  el.setAttribute('data-rs-original-text', text);
                }}
                if (lang) {{
                  el.setAttribute('data-rs-original-lang', lang);
                }}
                // Restore source-language body so screenshots keep the original tweet text.
                if (live && live !== text && looksMostlyChinese(live) && !looksMostlyChinese(text)) {{
                  el.textContent = text;
                  if (lang) {{
                    el.setAttribute('lang', lang);
                  }}
                }}
              }}
            }}
            """,
            {"text": original_text, "lang": lang or ""},
        )
    except Exception:
        pass


def _apply_oembed_originals_to_blocks(
    tweet_card,
    replacements: list[dict[str, str | int | None]],
) -> None:
    if not replacements:
        return
    try:
        tweet_card.evaluate(
            f"""
            (root, replacements) => {{
              const collectAnchors = {_TEXT_ANCHOR_COLLECTION_JS};
              const anchors = collectAnchors(root);
              for (const replacement of replacements || []) {{
                const index = Number(replacement.index);
                const anchor = anchors[index];
                const text = (replacement.text || '').trim();
                if (!anchor || !text) {{
                  continue;
                }}
                anchor.setAttribute('data-rs-original-text', text);
                if (replacement.lang) {{
                  anchor.setAttribute('data-rs-original-lang', replacement.lang);
                  anchor.setAttribute('lang', replacement.lang);
                }}
                anchor.textContent = text;
              }}
            }}
            """,
            replacements,
        )
    except Exception:
        pass


def _extract_quoted_status_urls(tweet_card, status_url: str | None = None) -> list[str]:
    main_id = _extract_status_id_from_url(status_url)
    try:
        urls = tweet_card.evaluate(
            """
            (root, mainId) => {
              const idFromHref = (href) => {
                const match = String(href || '').match(/\\/(?:i\\/(?:web\\/)?status|[^/?#]+\\/status)\\/(\\d+)/i);
                return match ? match[1] : '';
              };
              const normalize = (href) => {
                try {
                  const url = new URL(href, 'https://x.com');
                  return `${url.origin}${url.pathname}`;
                } catch (error) {
                  return href || '';
                }
              };

              const seen = new Set();
              const out = [];
              for (const anchor of root.querySelectorAll('a[href*="/status/"], a[href*="/i/web/status/"], a[href*="/i/status/"]')) {
                const href = anchor.href || anchor.getAttribute('href') || '';
                const id = idFromHref(href);
                if (!id || id === mainId || seen.has(id)) {
                  continue;
                }
                seen.add(id);
                out.push(normalize(href));
              }
              return out;
            }
            """,
            main_id or "",
        )
    except Exception:
        return []

    if not isinstance(urls, list):
        return []
    return [str(url).strip() for url in urls if str(url or "").strip()]


def _collect_translation_text_blocks(tweet_card, status_url: str | None = None) -> list[dict[str, str | int | None]]:
    """Extract tweet bodies for translation, with per-tweet oEmbed fallback for quoted posts."""
    _dismiss_x_auto_translation(tweet_card)
    text_blocks = _extract_translatable_text_blocks(tweet_card)

    replacements: list[dict[str, str | int | None]] = []
    for block in text_blocks:
        text = str(block.get("text") or "").strip()
        block_url = str(block.get("status_url") or status_url or "").strip()
        if not text or not block_url:
            continue
        if _text_looks_non_chinese(text):
            continue

        oembed_text, oembed_lang = _fetch_oembed_tweet_body(block_url)
        if not oembed_text or not _text_looks_non_chinese(oembed_text):
            continue

        block["text"] = oembed_text
        block["lang"] = oembed_lang or block.get("lang") or ""
        replacements.append(
            {
                "index": block.get("index"),
                "text": oembed_text,
                "lang": oembed_lang or "",
            }
        )

    if replacements:
        _apply_oembed_originals_to_blocks(tweet_card, replacements)
        text_blocks = _extract_translatable_text_blocks(tweet_card)

    seen_ids = {
        _extract_status_id_from_url(str(block.get("status_url") or status_url or ""))
        for block in text_blocks
    }
    seen_ids.discard(None)
    seen_texts = {str(block.get("text") or "").strip() for block in text_blocks if block.get("text")}
    for quoted_url in _extract_quoted_status_urls(tweet_card, status_url):
        quoted_id = _extract_status_id_from_url(quoted_url)
        if not quoted_id or quoted_id in seen_ids:
            continue
        oembed_text, oembed_lang = _fetch_oembed_tweet_body(quoted_url)
        if not oembed_text or not _text_looks_non_chinese(oembed_text):
            continue
        if oembed_text in seen_texts:
            continue
        text_blocks.append(
            {
                "index": len(text_blocks),
                "text": oembed_text,
                "lang": oembed_lang or "",
                "status_url": quoted_url,
            }
        )
        seen_ids.add(quoted_id)
        seen_texts.add(oembed_text)

    if any(_text_looks_non_chinese(str(block.get("text") or "")) for block in text_blocks):
        return text_blocks

    oembed_text, oembed_lang = _fetch_oembed_tweet_body(status_url or "")
    if not oembed_text or not _text_looks_non_chinese(oembed_text):
        return text_blocks

    _seed_original_text_from_oembed(tweet_card, oembed_text, oembed_lang)
    text_blocks = _extract_translatable_text_blocks(tweet_card)
    if any(_text_looks_non_chinese(str(block.get("text") or "")) for block in text_blocks):
        return text_blocks
    return [{"index": 0, "text": oembed_text, "lang": oembed_lang or ""}]


def _dismiss_x_auto_translation(tweet_card) -> None:
    """Dismiss X's built-in auto-translation by clicking '显示原文' / 'Show original' buttons.

    When the browser locale is zh-CN, X automatically translates non-Chinese tweets
    and replaces the original text in [data-testid="tweetText"] with the translation.
    This function reverts to the original text so our own translation pipeline works correctly.
    """
    try:
        clicked = tweet_card.evaluate(
            """
            (root) => {
              const labels = [
                '显示原文',
                '顯示原文',
                'Show original',
                'Ver original',
                '原文を表示',
                '원문 보기',
              ];
              let clicked = 0;
              const candidates = root.querySelectorAll('button, div[role="button"], span[role="button"]');
              for (const node of candidates) {
                const label = (
                  node.getAttribute('aria-label')
                  || node.textContent
                  || ''
                ).trim();
                if (!labels.some((item) => label === item || label.includes(item))) {
                  continue;
                }
                try {
                  node.click();
                  clicked += 1;
                } catch (error) {
                  // ignore
                }
              }
              return clicked;
            }
            """
        )
        if clicked:
            # Wait for DOM to update after reverting translations
            time.sleep(0.6)

        # Sync captured originals with restored DOM text — but never replace a
        # foreign-language snapshot with a Chinese auto-translation still on screen.
        tweet_card.evaluate(
            f"""
            (root) => {{
              const looksMostlyChinese = {_LOOKS_MOSTLY_CHINESE_JS};
              for (const el of root.querySelectorAll('[data-testid="tweetText"]')) {{
                const text = (el.innerText || '').trim();
                if (!text) {{
                  continue;
                }}
                const existing = (el.getAttribute('data-rs-original-text') || '').trim();
                if (existing && !looksMostlyChinese(existing) && looksMostlyChinese(text)) {{
                  continue;
                }}
                el.setAttribute('data-rs-original-text', text);
                const lang = el.getAttribute('lang');
                if (lang) {{
                  el.setAttribute('data-rs-original-lang', lang);
                }} else if (!(existing && !looksMostlyChinese(existing))) {{
                  el.removeAttribute('data-rs-original-lang');
                }}
              }}
            }}
            """
        )
    except Exception:
        pass


_TEXT_ANCHOR_COLLECTION_JS = f"""
(root) => {{
  const isVisible = (node) => {{
    if (!(node instanceof Element)) {{
      return false;
    }}
    const style = window.getComputedStyle(node);
    if (!style) {{
      return false;
    }}
    if (style.display === 'none' || style.visibility === 'hidden') {{
      return false;
    }}
    const rect = node.getBoundingClientRect();
    return rect.width >= 8 && rect.height >= 8;
  }};

  const isTweetBodyText = (node) => {{
    if (!(node instanceof Element)) {{
      return false;
    }}
    const quotedRoot = node.closest('[data-testid="quoteTweet"]');
    if (node.closest('[data-testid="User-Name"]')) {{
      return false;
    }}
    if (node.closest('[data-testid="socialContext"]')) {{
      return false;
    }}
    if (node.closest('[role="group"]') && !quotedRoot) {{
      return false;
    }}
    if (node.closest('[{TRANSLATION_ATTR}="block"]')) {{
      return false;
    }}
    return true;
  }};

  const seen = new Set();
  const anchors = [];
  const pushNode = (node) => {{
    const text = (node.innerText || '').trim();
    if (!text || seen.has(text)) {{
      return;
    }}
    // Skip text that contains no letters (only numbers, punctuation, whitespace)
    if (!/\\p{{L}}/u.test(text)) {{
      return;
    }}
    // Accept tweet body text elements with low threshold.
    // Non-body-text elements (usernames, buttons, etc.) are already excluded by isTweetBodyText.
    const minLen = 4;
    if (text.length < minLen) {{
      return;
    }}
    seen.add(text);
    anchors.push(node);
  }};

  const selectors = [
    '[data-testid="tweetText"]',
    'div[dir="auto"]',
  ];
  for (const selector of selectors) {{
    for (const node of root.querySelectorAll(selector)) {{
      if (!isVisible(node) || !isTweetBodyText(node)) {{
        continue;
      }}
      pushNode(node);
    }}
  }}

  return anchors;
}}
"""


def _extract_translatable_text_blocks(tweet_card) -> list[dict[str, str | int | None]]:
    try:
        blocks = tweet_card.evaluate(
            f"""
            (root) => {{
              const collectAnchors = {_TEXT_ANCHOR_COLLECTION_JS};
              const anchors = collectAnchors(root);
              const looksMostlyChinese = {_LOOKS_MOSTLY_CHINESE_JS};

              return anchors.map((node, index) => {{
                const quotedRoot = node.closest('[data-testid="quoteTweet"]');
                const statusAnchor = quotedRoot
                  ? (
                    node.closest('a[href*="/status/"], a[href*="/i/web/status/"]')
                    || quotedRoot.querySelector('a[href*="/status/"], a[href*="/i/web/status/"]')
                  )
                  : null;
                const statusUrl = statusAnchor?.href || '';
                // Prefer the original text captured by MutationObserver before X auto-translated it,
                // but if "Show original" restored a non-Chinese body, trust the live DOM instead.
                const origText = (node.getAttribute('data-rs-original-text') || '').trim();
                const liveText = (node.innerText || '').trim();
                let text = liveText || origText;
                if (origText && liveText && origText !== liveText) {{
                  if (looksMostlyChinese(liveText) && !looksMostlyChinese(origText)) {{
                    text = origText;
                  }} else if (!looksMostlyChinese(liveText) && looksMostlyChinese(origText)) {{
                    text = liveText;
                  }} else {{
                    text = liveText;
                  }}
                }}
                const origLang = node.getAttribute('data-rs-original-lang');
                const liveLang = node.getAttribute('lang')
                  || node.querySelector('[lang]')?.getAttribute('lang')
                  || '';
                // If we chose non-Chinese live text over a Chinese snapshot, drop a stale zh lang tag.
                let lang = origLang || liveLang || '';
                if (text === liveText && liveLang) {{
                  lang = liveLang;
                }}
                if (text === liveText && looksMostlyChinese(origText) && !looksMostlyChinese(liveText)) {{
                  lang = liveLang || '';
                }}
                if (text === origText && origLang) {{
                  lang = origLang;
                }}
                return {{ index, text, lang, status_url: statusUrl }};
              }});
            }}
            """
        )
    except Exception:
        return []

    if not isinstance(blocks, list):
        return []
    return blocks


def _translate_text_to_chinese_via_google(text: str, source_lang: str | None) -> str | None:
    normalized_text = (text or "").strip()
    normalized_lang = _normalize_translation_lang(source_lang) or "auto"
    if not normalized_text:
        return None

    query = urlencode(
        {
            "client": "gtx",
            "sl": normalized_lang,
            "tl": "zh-CN",
            "dt": "t",
            "q": normalized_text,
        }
    )
    payload = _fetch_translation_payload(f"{GOOGLE_TRANSLATE_API_URL}?{query}")
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], list):
        return None

    translated_parts: list[str] = []
    for part in payload[0]:
        if not isinstance(part, list) or not part:
            continue
        if isinstance(part[0], str) and part[0]:
            translated_parts.append(part[0])

    translated = "".join(translated_parts).strip()
    if not translated:
        return None
    if translated.casefold() == normalized_text.casefold():
        return None
    return translated


def _translate_text_to_chinese_via_mymemory(text: str, source_lang: str | None) -> str | None:
    normalized_text = (text or "").strip()
    normalized_lang = _normalize_translation_lang(source_lang) or _detect_translation_source_lang(
        normalized_text
    )
    if not normalized_text or not normalized_lang:
        return None

    query = urlencode({"q": normalized_text, "langpair": f"{normalized_lang}|zh-CN"})
    payload = _fetch_translation_payload(f"{MYMEMORY_TRANSLATE_API_URL}?{query}")
    if not isinstance(payload, dict):
        return None

    if payload.get("responseStatus") != 200:
        return None

    translated = unescape(str(payload.get("responseData", {}).get("translatedText") or "")).strip()
    if not translated:
        return None
    if translated.casefold() == normalized_text.casefold():
        return None
    return translated


def _translate_text_to_chinese(text: str, source_lang: str | None) -> str | None:
    normalized_text = (text or "").strip()
    normalized_lang = _normalize_translation_lang(source_lang)
    if not normalized_text or (normalized_lang and normalized_lang.startswith("zh")):
        return None
    if not normalized_lang:
        normalized_lang = _detect_translation_source_lang(normalized_text)

    translated = _translate_text_to_chinese_via_google(normalized_text, normalized_lang)
    if translated:
        return translated
    return _translate_text_to_chinese_via_mymemory(normalized_text, normalized_lang)


def _split_custom_translation_blocks(custom_translation: str | None) -> tuple[list[str], dict[int, str]]:
    normalized = (custom_translation or "").strip()
    if not normalized:
        return [], {}

    named_overrides: dict[int, str] = {}
    current_target: int | None = None
    current_lines: list[str] = []
    saw_named_override = False

    def flush_named_override() -> None:
        nonlocal current_target, current_lines
        if current_target is None:
            current_lines = []
            return
        content = "\n".join(current_lines).strip()
        if content:
            named_overrides[current_target] = content
        current_target = None
        current_lines = []

    label_map = {
        "主帖": 0,
        "正文": 0,
        "原帖": 0,
        "引用": 1,
        "引用贴": 1,
    }

    for line in normalized.splitlines():
        match = re.match(r"^\s*(主帖|正文|原帖|引用|引用贴)\s*[:：]\s*(.*)$", line)
        if match:
            saw_named_override = True
            flush_named_override()
            current_target = label_map[match.group(1)]
            remainder = match.group(2).strip()
            current_lines = [remainder] if remainder else []
            continue

        if current_target is not None:
            current_lines.append(line)

    flush_named_override()
    if saw_named_override:
        return [], named_overrides

    parts = [
        part.strip()
        for part in re.split(r"(?:\r?\n\s*){2,}", normalized)
        if part.strip()
    ]
    return parts, {}


def _build_translation_items(
    text_blocks: list[dict[str, str | int | None]],
    *,
    translation_overrides: dict[int, str] | None = None,
    custom_translation: str | None = None,
) -> list[dict[str, str | int]]:
    custom_translation_blocks, custom_translation_overrides = _split_custom_translation_blocks(custom_translation)
    overrides = {int(index): str(value) for index, value in (translation_overrides or {}).items()}
    overrides.update(custom_translation_overrides)

    cache: dict[tuple[str, str], str | None] = {}
    items: list[dict[str, str | int]] = []
    for index, block in enumerate(text_blocks):
        text = str(block.get("text") or "").strip()
        lang = _normalize_translation_lang(block.get("lang"))
        if not text:
            continue

        translation: str | None
        if index in overrides:
            translation = str(overrides[index]).strip()
        elif index < len(custom_translation_blocks):
            translation = custom_translation_blocks[index]
        else:
            # X may tag restored Spanish/English/ja/ko bodies as lang=zh after auto-translate.
            # Skip only when the text itself is predominantly Chinese.
            if lang and lang.startswith("zh") and not _text_looks_non_chinese(text):
                continue
            if not lang and _looks_like_chinese_text(text):
                continue
            # When lang is wrongly zh but body is foreign, force auto language detection.
            if lang and lang.startswith("zh") and _text_looks_non_chinese(text):
                effective_lang = "auto"
            else:
                effective_lang = lang or _detect_translation_source_lang(text)
            cache_key = (text, effective_lang or "auto")
            if cache_key not in cache:
                cache[cache_key] = _translate_text_to_chinese(
                    text,
                    None if effective_lang in (None, "auto") else effective_lang,
                )
            translation = cache[cache_key]
            # Keep non-Chinese body text in the review UI even if providers fail,
            # so the user can fill in a manual translation.
            if not translation and effective_lang and not str(effective_lang).startswith("zh"):
                translation = ""

        if translation is None:
            continue
        if translation and translation.casefold() == text.casefold():
            continue

        items.append(
            {
                "index": int(block["index"]),
                "text": text,
                "translation": translation,
                "status_url": str(block.get("status_url") or ""),
            }
        )

    return items


def _inject_chinese_translations(
    tweet_card,
    custom_translation: str | None = None,
    translation_overrides: dict[int, str] | None = None,
    status_url: str | None = None,
) -> int:
    text_blocks = _collect_translation_text_blocks(tweet_card, status_url)
    if not text_blocks:
        return 0

    items = _build_translation_items(
        text_blocks,
        translation_overrides=translation_overrides,
        custom_translation=custom_translation,
    )
    if not items:
        return 0

    try:
        inserted = tweet_card.evaluate(
            f"""
            (root, entries) => {{
              const collectAnchors = {_TEXT_ANCHOR_COLLECTION_JS};
              const statusIdFromUrl = (href) => {{
                const match = String(href || '').match(/\\/(?:i\\/(?:web\\/)?status|[^/?#]+\\/status)\\/(\\d+)/i);
                return match ? match[1] : '';
              }};
              const findAnchorByStatusUrl = (statusUrl) => {{
                const targetId = statusIdFromUrl(statusUrl);
                if (!targetId) {{
                  return null;
                }}
                const link = [...root.querySelectorAll('a[href*="/status/"], a[href*="/i/web/status/"], a[href*="/i/status/"]')]
                  .find((anchor) => statusIdFromUrl(anchor.href || anchor.getAttribute('href')) === targetId);
                if (!link) {{
                  return null;
                }}
                const quoteRoot = link.closest('[data-testid="quoteTweet"], [data-testid="card.wrapper"], div[role="link"]');
                if (!quoteRoot) {{
                  return link;
                }}
                const textNode = quoteRoot.querySelector('[data-testid="tweetText"], div[dir="auto"]');
                return textNode || quoteRoot;
              }};

              root.querySelectorAll('[{TRANSLATION_ATTR}="block"]').forEach((node) => node.remove());
              const blocks = collectAnchors(root);
              let count = 0;

              for (const entry of entries) {{
                const anchor =
                  blocks[entry.index] ||
                  blocks.find((node) => (node.innerText || '').trim() === (entry.text || '').trim()) ||
                  findAnchorByStatusUrl(entry.status_url || '');
                if (!anchor || !entry.translation) {{
                  continue;
                }}

                const wrapper = document.createElement('div');
                wrapper.setAttribute('{TRANSLATION_ATTR}', 'block');

                const label = document.createElement('span');
                label.setAttribute('{TRANSLATION_ATTR}', 'label');
                label.textContent = '中文翻译';

                const body = document.createElement('span');
                body.setAttribute('{TRANSLATION_ATTR}', 'body');
                body.textContent = entry.translation;

                wrapper.append(label, body);
                anchor.insertAdjacentElement('afterend', wrapper);
                count += 1;
              }}

              return count;
            }}
            """,
            items,
        )
    except Exception:
        return 0

    if isinstance(inserted, int):
        return inserted
    return 0


def _remove_native_translation_ui(tweet_card) -> None:
    try:
        tweet_card.evaluate(
            """
            (root) => {
              const exactTexts = new Set([
                '显示翻译',
                'Translate post',
                'Translate Tweet',
                'Show translation',
                '查看翻译',
                '显示原文',
                'Show original',
                '重试',
              ]);
              const blockTexts = ['无法获取翻译', '翻译自', 'Translated from', '评价此翻译', 'Rate this translation'];

              const candidates = [
                ...root.querySelectorAll('button, [role="button"], a, div, span'),
              ];

              for (const node of candidates) {
                if (!(node instanceof HTMLElement)) {
                  continue;
                }
                const text = (node.innerText || '').trim();
                if (!text) {
                  continue;
                }
                const matchesExact = exactTexts.has(text);
                const matchesBlock = blockTexts.some((value) => text.includes(value));
                if (!matchesExact && !matchesBlock) {
                  continue;
                }
                if (node.hasAttribute('data-testid') && node.getAttribute('data-testid') === 'tweetText') {
                  continue;
                }

                // Remove the entire translation bar container when we match '翻译自' / 'Translated from'
                if (matchesBlock && (text.includes('翻译自') || text.includes('Translated from'))) {
                  const bar = node.closest('.css-175oi2r.r-1s2bzr4') || node.closest('.css-175oi2r') || node;
                  bar.remove();
                  continue;
                }

                const target = matchesExact
                  ? node.closest('button, [role="button"], a') || node
                  : node;
                target.remove();
              }
            }
            """
        )
    except Exception:
        return


def _translation_label_for_index(index: int) -> str:
    if index == 0:
        return "主帖正文"
    if index == 1:
        return "引用贴正文"
    return f"第 {index + 1} 段正文"


def _parse_video_timestamp(value: object | None) -> float | None:
    raw = "" if value is None else str(value).strip()
    if not raw:
        return None

    parts = raw.split(":")
    if len(parts) > 3 or any(part.strip() == "" for part in parts):
        raise ValueError("视频时间点格式不太对，可以填 2、10.5 或 01:23")

    total_seconds = 0.0
    multiplier = 1.0
    try:
        for part in reversed(parts):
            amount = float(part)
            if amount < 0:
                raise ValueError
            total_seconds += amount * multiplier
            multiplier *= 60.0
    except ValueError as exc:
        raise ValueError("视频时间点格式不太对，可以填 2、10.5 或 01:23") from exc

    return total_seconds


def _split_video_timestamp_input(raw: str | None) -> tuple[list[str], dict[int, str]]:
    normalized = (raw or "").strip()
    if not normalized:
        return [], {}

    named: dict[int, str] = {}
    sequential: list[str] = []
    saw_named = False
    label_map = {
        "主帖": 0,
        "正文": 0,
        "原帖": 0,
        "引用": 1,
        "引用贴": 1,
    }

    for line in normalized.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = re.match(r"^\s*(主帖|正文|原帖|引用|引用贴)\s*[:：]\s*(.*)$", stripped)
        if match:
            saw_named = True
            time_part = match.group(2).strip()
            if time_part:
                named[label_map[match.group(1)]] = time_part
            continue
        if not saw_named:
            sequential.append(stripped)

    if saw_named:
        return [], named
    return sequential, {}


def parse_video_timestamps_from_request(
    video_time: object | None = None,
    video_times: object | None = None,
) -> dict[str, dict[str, float | None]]:
    chunks: list[str] = []
    if video_times is not None:
        if isinstance(video_times, list):
            chunks.extend(str(item) for item in video_times if str(item).strip())
        else:
            text = str(video_times).strip()
            if text:
                chunks.append(text)
    if video_time is not None and str(video_time).strip():
        legacy = str(video_time).strip()
        if not chunks:
            chunks.append(legacy)

    sequential, named = _split_video_timestamp_input("\n".join(chunks))
    by_index: dict[str, float | None] = {}
    for index, part in enumerate(sequential):
        parsed = _parse_video_timestamp(part)
        if parsed is not None:
            by_index[str(index)] = parsed

    named_out: dict[str, float | None] = {}
    if 0 in named:
        named_out["main"] = _parse_video_timestamp(named[0])
    if 1 in named:
        named_out["quote"] = _parse_video_timestamp(named[1])

    return {"byIndex": by_index, "named": named_out}


def _video_frame_label(*, quoted: bool, main_ordinal: int, quote_ordinal: int) -> str:
    if not quoted:
        if main_ordinal == 0:
            return "主帖视频"
        return f"主帖视频 {main_ordinal + 1}"
    if quote_ordinal == 0:
        return "引用贴视频"
    return f"引用贴视频 {quote_ordinal + 1}"


def _prepare_video_frames(
    tweet_card,
    schedule: dict[str, dict[str, float | None]] | None,
) -> tuple[VideoFrameInfo, ...]:
    payload = schedule or {"byIndex": {}, "named": {}}
    try:
        result = tweet_card.evaluate(
            """
            async (root, schedule) => {
              const byIndex = schedule?.byIndex || {};
              const named = schedule?.named || {};
              const isVisible = (node) => {
                if (!(node instanceof Element)) {
                  return false;
                }
                const style = window.getComputedStyle(node);
                if (!style) {
                  return false;
                }
                if (style.display === 'none' || style.visibility === 'hidden') {
                  return false;
                }
                const rect = node.getBoundingClientRect();
                return rect.width >= 48 && rect.height >= 48;
              };

              const wait = (ms) => new Promise((resolve) => window.setTimeout(resolve, ms));

              const waitUntil = async (predicate, timeoutMs) => {
                const deadline = Date.now() + timeoutMs;
                while (Date.now() < deadline) {
                  try {
                    if (predicate()) {
                      return true;
                    }
                  } catch (error) {
                  }
                  await wait(80);
                }
                return false;
              };

              const once = (target, eventName, timeoutMs) =>
                new Promise((resolve) => {
                  let settled = false;
                  const finish = () => {
                    if (settled) {
                      return;
                    }
                    settled = true;
                    target.removeEventListener(eventName, onEvent);
                    window.clearTimeout(timer);
                    resolve();
                  };
                  const onEvent = () => finish();
                  const timer = window.setTimeout(finish, timeoutMs);
                  target.addEventListener(eventName, onEvent, { once: true });
                });

              const isInQuotedTweet = (node) =>
                Boolean(
                  node.closest('[data-testid="card.wrapper"]') ||
                    node.closest('[data-testid="quoteTweet"]'),
                );

              const videos = [...root.querySelectorAll('video')]
                .filter((video) => isVisible(video))
                .map((video) => {
                  const rect = video.getBoundingClientRect();
                  return {
                    video,
                    area: rect.width * rect.height,
                    quoted: isInQuotedTweet(video),
                  };
                });

              if (videos.length === 0) {
                return [];
              }

              const sortByPosition = (a, b) => {
                const rectA = a.video.getBoundingClientRect();
                const rectB = b.video.getBoundingClientRect();
                if (Math.abs(rectA.top - rectB.top) > 8) {
                  return rectA.top - rectB.top;
                }
                return rectA.left - rectB.left;
              };

              const mainVideos = videos.filter((entry) => !entry.quoted).sort(sortByPosition);
              const quotedVideos = videos.filter((entry) => entry.quoted).sort(sortByPosition);
              const ordered = [...mainVideos, ...quotedVideos];
              const firstMainIdx = ordered.findIndex((entry) => !entry.quoted);
              const firstQuoteIdx = ordered.findIndex((entry) => entry.quoted);

              const resolveTargetSeconds = (index, quoted) => {
                const key = String(index);
                if (
                  Object.prototype.hasOwnProperty.call(byIndex, key) &&
                  byIndex[key] !== null &&
                  byIndex[key] !== undefined
                ) {
                  return byIndex[key];
                }
                if (
                  !quoted &&
                  index === firstMainIdx &&
                  named.main !== null &&
                  named.main !== undefined
                ) {
                  return named.main;
                }
                if (
                  quoted &&
                  index === firstQuoteIdx &&
                  named.quote !== null &&
                  named.quote !== undefined
                ) {
                  return named.quote;
                }
                return null;
              };

              const findVideoPlayerRoot = (video, cardRoot) => {
                const explicit =
                  video.closest('[data-testid="videoComponent"]') ||
                  video.closest('[data-testid="videoPlayer"]');
                if (explicit instanceof HTMLElement) {
                  return explicit;
                }

                const videoRect = video.getBoundingClientRect();
                let node = video.parentElement;
                while (node instanceof HTMLElement && node !== cardRoot) {
                  const rect = node.getBoundingClientRect();
                  if (
                    rect.width <= Math.max(videoRect.width * 1.8, videoRect.width + 96) &&
                    rect.height <= Math.max(videoRect.height * 2.5, videoRect.height + 96)
                  ) {
                    return node;
                  }
                  node = node.parentElement;
                }

                return video.parentElement instanceof HTMLElement ? video.parentElement : video;
              };

              const prepareOneVideo = async (video, targetSeconds, cardRoot) => {
              video.muted = true;
              video.defaultMuted = true;
              video.playsInline = true;
              video.preload = 'auto';
              video.controls = false;

              const playerRoot = findVideoPlayerRoot(video, cardRoot);

              const activatePlayer = () => {
                const candidates = [
                  playerRoot?.querySelector('button[aria-label*="Play"]'),
                  playerRoot?.querySelector('button[aria-label*="播放"]'),
                  playerRoot?.querySelector('[role="button"][aria-label*="Play"]'),
                  playerRoot?.querySelector('[role="button"][aria-label*="播放"]'),
                  playerRoot?.querySelector('button'),
                  video,
                ].filter(Boolean);
                for (const node of candidates) {
                  try {
                    if (node instanceof HTMLElement) {
                      node.click();
                      return true;
                    }
                  } catch (error) {
                  }
                }
                return false;
              };

              const ensureMetadata = async () => {
                if (video.readyState >= 1) {
                  return;
                }
                if (video.readyState === 0 && typeof video.load === 'function') {
                  try {
                    video.load();
                  } catch (error) {
                  }
                }
                await Promise.race([
                  once(video, 'loadedmetadata', 5000),
                  once(video, 'durationchange', 5000),
                  once(video, 'loadeddata', 5000),
                ]);
              };

              const playFor = async (ms) => {
                try {
                  activatePlayer();
                  const playPromise = video.play();
                  if (playPromise && typeof playPromise.then === 'function') {
                    await Promise.race([playPromise.catch(() => undefined), wait(250)]);
                  } else {
                    await wait(250);
                  }
                } catch (error) {
                }
                await wait(ms);
              };

              const pauseVideo = () => {
                try {
                  video.pause();
                } catch (error) {
                }
              };

              const warmUp = async () => {
                try {
                  await playFor(1200);
                } finally {
                  pauseVideo();
                  await wait(180);
                }
              };

              const waitForDecodedFrame = async (minWidth = 640, timeoutMs = 8000) => {
                await waitUntil(
                  () =>
                    !video.error &&
                    video.readyState >= 2 &&
                    Number.isFinite(video.videoWidth) &&
                    video.videoWidth >= minWidth,
                  timeoutMs,
                );
              };

              const clampTime = (value, duration) => {
                if (!Number.isFinite(value) || value < 0) {
                  return 0;
                }
                if (duration === null) {
                  return value;
                }
                return Math.min(value, Math.max(duration - 0.12, 0));
              };

              const seekTo = async (value, duration) => {
                const nextTime = clampTime(value, duration);
                if (Math.abs((video.currentTime || 0) - nextTime) <= 0.04) {
                  return nextTime;
                }
                try {
                  const seekPromise = Promise.race([
                    once(video, 'seeking', 1200),
                    once(video, 'seeked', 5000),
                    once(video, 'timeupdate', 5000),
                  ]);
                  video.currentTime = nextTime;
                  await seekPromise;
                  await waitUntil(
                    () => Math.abs((video.currentTime || 0) - nextTime) <= 0.18,
                    2500,
                  );
                  await waitForDecodedFrame(Math.min(video.videoWidth || 640, 640), 5000);
                } catch (error) {
                }
                return nextTime;
              };

              const hideVideoOverlays = () => {
                video.controls = false;

                const root = playerRoot instanceof HTMLElement ? playerRoot : video.parentElement;
                if (!(root instanceof HTMLElement) || root === cardRoot) {
                  return;
                }
                if (/(views|查看|次观看)/i.test(root.innerText || '')) {
                  return;
                }

                root.querySelectorAll(
                  'button,[role="button"],[role="progressbar"],[role="slider"],input[type="range"],svg,img',
                ).forEach((node) => {
                  if (!(node instanceof HTMLElement) || node === video || video.contains(node)) {
                    return;
                  }
                  node.style.setProperty('display', 'none', 'important');
                  node.style.setProperty('opacity', '0', 'important');
                  node.style.setProperty('visibility', 'hidden', 'important');
                });

                const rootRect = root.getBoundingClientRect();
                for (const node of root.querySelectorAll('div')) {
                  if (!(node instanceof HTMLElement) || node === video || video.contains(node) || node.contains(video)) {
                    continue;
                  }
                  const rect = node.getBoundingClientRect();
                  if (rect.width < rootRect.width * 0.45 || rect.height < 4 || rect.height > 96) {
                    continue;
                  }
                  if (rect.bottom >= rootRect.bottom - 8 && rect.top >= rootRect.bottom - 100) {
                    node.style.setProperty('display', 'none', 'important');
                    node.style.setProperty('background', 'transparent', 'important');
                  }
                }

                const videoRect = video.getBoundingClientRect();
                if (videoRect.height > 0) {
                  const topOffset = Math.max(0, videoRect.top - rootRect.top);
                  const targetHeight = Math.ceil(topOffset + videoRect.height);
                  root.style.height = `${targetHeight}px`;
                  root.style.maxHeight = `${targetHeight}px`;
                  root.style.overflow = 'hidden';
                  root.style.background = 'transparent';
                  root.style.paddingBottom = '0';
                }
              };

              const renderTargetFrame = async (desiredTime, duration) => {
                try {
                  activatePlayer();
                  const playPromise = video.play();
                  if (playPromise && typeof playPromise.then === 'function') {
                    await Promise.race([playPromise.catch(() => undefined), wait(400)]);
                  } else {
                    await wait(250);
                  }

                  await waitUntil(
                    () => {
                      const current = video.currentTime || 0;
                      return current >= Math.max(desiredTime - 0.12, 0);
                    },
                    3500,
                  );
                } catch (error) {
                } finally {
                  pauseVideo();
                  await wait(220);
                }

                if (Math.abs((video.currentTime || 0) - desiredTime) > 0.35) {
                  await seekTo(desiredTime, duration);
                  pauseVideo();
                  await wait(180);
                }
              };

              await ensureMetadata();
              // High-bitrate HLS needs a real decode pass before seeking,
              // otherwise Chromium often ends up with MEDIA_ERR_SRC_NOT_SUPPORTED.
              // Portrait clips may only be ~720px wide, so don't require 640+.
              const minDecodeWidth = 360;
              await warmUp();
              await waitForDecodedFrame(minDecodeWidth, 8000);
              if (video.readyState < 2 || video.videoWidth < minDecodeWidth) {
                await warmUp();
                await waitForDecodedFrame(minDecodeWidth, 8000);
              }

              // If 4K/unsupported streams still fail, leave the player alone —
              // a later poster/thumbnail fallback can cover the screenshot.
              if (video.error || video.readyState < 1 || video.videoWidth < 2) {
                hideVideoOverlays();
                return null;
              }

              const duration = Number.isFinite(video.duration) && video.duration > 0 ? video.duration : null;
              let desiredTime = null;
              if (Number.isFinite(targetSeconds) && targetSeconds >= 0) {
                desiredTime = targetSeconds;
              } else if (duration !== null) {
                desiredTime = Math.min(
                  Math.max(duration * 0.18, 0.8),
                  3.5,
                  Math.max(duration - 0.12, 0),
                );
              } else {
                desiredTime = 0.8;
              }

              desiredTime = clampTime(desiredTime, duration);
              await seekTo(desiredTime, duration);
              if (video.readyState < 2 || video.videoWidth < minDecodeWidth) {
                await warmUp();
                await seekTo(desiredTime, duration);
              }

              await renderTargetFrame(desiredTime, duration);
              if (Math.abs((video.currentTime || 0) - desiredTime) > 0.35) {
                await seekTo(desiredTime, duration);
                await renderTargetFrame(desiredTime, duration);
              }

              await waitForDecodedFrame(minDecodeWidth, 6000);
              await wait(280);

              hideVideoOverlays();
              await wait(160);
              return Number.isFinite(video.currentTime) ? video.currentTime : desiredTime;
              };

              const frameResults = [];
              for (let index = 0; index < ordered.length; index += 1) {
                const entry = ordered[index];
                const seconds = await prepareOneVideo(
                  entry.video,
                  resolveTargetSeconds(index, entry.quoted),
                  root,
                );
                frameResults.push({
                  index,
                  quoted: entry.quoted,
                  seconds,
                });
              }

              for (const entry of ordered) {
                try {
                  entry.video.pause();
                } catch (error) {
                }
              }

              return frameResults;
            }
            """,
            payload,
        )
    except Exception:
        return ()

    if not isinstance(result, list):
        return ()

    frames: list[VideoFrameInfo] = []
    main_ordinal = 0
    quote_ordinal = 0
    for item in result:
        if not isinstance(item, dict):
            continue
        try:
            slot_index = int(item.get("index", 0))
            quoted = bool(item.get("quoted"))
            seconds = float(item.get("seconds", 0))
        except (TypeError, ValueError):
            continue
        if seconds < 0:
            continue

        if quoted:
            label = _video_frame_label(
                quoted=True,
                main_ordinal=0,
                quote_ordinal=quote_ordinal,
            )
            quote_ordinal += 1
        else:
            label = _video_frame_label(
                quoted=False,
                main_ordinal=main_ordinal,
                quote_ordinal=0,
            )
            main_ordinal += 1

        frames.append(
            VideoFrameInfo(
                index=slot_index,
                label=label,
                seconds=max(0.0, seconds),
                quoted=quoted,
            )
        )

    return tuple(frames)


def _prepare_tweet_media_for_screenshot(tweet_card) -> None:
    try:
        tweet_card.evaluate(
            """
            (root) => {
              const GRID_ATTR = 'data-resource-snapshot-media-grid';

              const isAvatarImage = (img) => {
                if (!(img instanceof HTMLImageElement)) {
                  return false;
                }
                return Boolean(
                  img.closest(
                    '[data-testid="Tweet-User-Avatar"], [data-testid="UserAvatar-Container-Unknown"], [data-testid="User-Name"]',
                  ),
                );
              };

              const isMediaImage = (img) => {
                if (!(img instanceof HTMLImageElement)) {
                  return false;
                }
                if (isAvatarImage(img)) {
                  return false;
                }
                return img.naturalWidth > 80;
              };

              const removeNode = (node) => {
                if (node instanceof Element) {
                  node.remove();
                }
              };

              for (const btn of root.querySelectorAll('button, [role="button"]')) {
                const label = (btn.getAttribute('aria-label') || btn.textContent || '').trim();
                if (/^(next|previous|上一|下一|次|前)/i.test(label)) {
                  removeNode(btn);
                }
              }

              root.querySelectorAll(`[${GRID_ATTR}]`).forEach(removeNode);

              // Detect and fix multi-cam video layouts (multiple simultaneous video panels)
              const allVideos = [...root.querySelectorAll('video')];
              if (allVideos.length > 1) {
                // Find the common ancestor container that holds all video panels
                const videoContainers = new Map();
                for (const video of allVideos) {
                  let parent = video.parentElement;
                  while (parent && parent !== root) {
                    const videos = parent.querySelectorAll('video');
                    if (videos.length > 1) {
                      const key = parent;
                      if (!videoContainers.has(key)) {
                        videoContainers.set(key, videos.length);
                      }
                      break;
                    }
                    parent = parent.parentElement;
                  }
                }

                // Find the innermost container with multiple videos
                let multiCamContainer = null;
                let minVideoCount = Infinity;
                for (const [container, count] of videoContainers) {
                  if (count > 1 && count < minVideoCount) {
                    multiCamContainer = container;
                    minVideoCount = count;
                  }
                }

                if (multiCamContainer instanceof HTMLElement) {
                  // Ensure the multi-cam container fits within the article
                  multiCamContainer.style.overflow = 'hidden';
                  multiCamContainer.style.width = '100%';
                  multiCamContainer.style.maxWidth = '100%';

                  // Also ensure all ancestor containers don't overflow
                  let ancestor = multiCamContainer.parentElement;
                  while (ancestor instanceof HTMLElement && ancestor !== root) {
                    ancestor.style.overflow = 'hidden';
                    ancestor.style.maxWidth = '100%';
                    ancestor = ancestor.parentElement;
                  }
                }
              }

              const carousels = [...root.querySelectorAll('div')].filter((node) => {
                if (!(node instanceof HTMLElement)) {
                  return false;
                }
                const cls = typeof node.className === 'string' ? node.className : '';
                return cls.includes('snap-x') && cls.includes('snap-mandatory');
              });

              const upgradeTwimgUrl = (url, name = 'large') => {
                if (!url || typeof url !== 'string') {
                  return url;
                }
                if (!/pbs\\.twimg\\.com\\/media\\//i.test(url)) {
                  return url;
                }
                if (/[?&]name=/.test(url)) {
                  return url.replace(/([?&]name=)[^&]*/i, `$1${name}`);
                }
                return `${url}${url.includes('?') ? '&' : '?'}name=${name}`;
              };

              const buildGridCell = (img) => {
                const cell = document.createElement('div');
                cell.style.position = 'relative';
                cell.style.overflow = 'hidden';
                cell.style.minWidth = '0';
                cell.style.minHeight = '0';
                cell.style.width = '100%';
                cell.style.height = '100%';

                const clone = img.cloneNode(true);
                clone.removeAttribute('style');
                clone.className = '';
                if (clone instanceof HTMLImageElement) {
                  const upgraded = upgradeTwimgUrl(clone.currentSrc || clone.src, 'large');
                  if (upgraded) {
                    clone.src = upgraded;
                  }
                  clone.removeAttribute('srcset');
                  clone.sizes = '100vw';
                }
                clone.style.width = '100%';
                clone.style.height = '100%';
                clone.style.objectFit = 'cover';
                clone.style.objectPosition = 'center center';
                clone.style.display = 'block';
                cell.appendChild(clone);
                return cell;
              };

              // Carousel parents keep a short landscape frame. After we rebuild a
              // taller 2x2 grid, unlock those ancestors or the bottom row gets
              // clipped into "forehead-only" peeks.
              const unlockMediaAncestors = (mediaEl) => {
                if (!(mediaEl instanceof HTMLElement)) {
                  return;
                }
                let node = mediaEl.parentElement;
                while (node instanceof HTMLElement && node !== root) {
                  node.style.height = 'auto';
                  node.style.maxHeight = 'none';
                  node.style.minHeight = '0';
                  node.style.aspectRatio = 'auto';
                  node.style.flex = '0 0 auto';
                  node.style.maxWidth = '100%';
                  node.style.width = '100%';
                  if (node !== mediaEl) {
                    const overflowY = window.getComputedStyle(node).overflowY;
                    if (overflowY === 'hidden' || overflowY === 'clip') {
                      node.style.overflow = 'visible';
                      node.style.overflowY = 'visible';
                    }
                  }
                  node = node.parentElement;
                }
              };

              const mountPhotoGrid = (carousel, grid, imageCount) => {
                if (imageCount <= 1) {
                  return;
                }
                if (imageCount === 2) {
                  grid.style.aspectRatio = '16 / 9';
                } else if (imageCount === 3) {
                  grid.style.aspectRatio = '4 / 3';
                } else {
                  // Match X's 4-photo mosaic more closely than a perfect square.
                  grid.style.aspectRatio = '7 / 8';
                }
                grid.style.height = 'auto';
                grid.style.minHeight = '0';
                grid.style.maxHeight = 'none';
                carousel.replaceWith(grid);
                unlockMediaAncestors(grid);

                // Force a concrete height from the final width so 1fr rows cannot
                // collapse inside a still-constrained parent during layout.
                const width = grid.getBoundingClientRect().width;
                if (width > 1) {
                  const ratio = imageCount === 2 ? 9 / 16 : imageCount === 3 ? 3 / 4 : 8 / 7;
                  const height = Math.round(width * ratio);
                  grid.style.height = `${height}px`;
                  grid.style.aspectRatio = 'auto';
                }
              };

              for (const carousel of carousels) {
                const slides = [...carousel.children].filter((child) => child.querySelector('img'));
                const images = slides
                  .map((slide) => slide.querySelector('img'))
                  .filter((img) => isMediaImage(img));

                if (images.length === 0) {
                  // Handle video carousels
                  const videoSlides = [...carousel.children].filter(
                    (child) => child.querySelector('video') || child.querySelector('[data-testid="videoComponent"]')
                  );
                  if (videoSlides.length > 1) {
                    // Multi-cam: convert horizontal carousel to grid layout
                    const grid = document.createElement('div');
                    grid.style.display = 'grid';
                    grid.style.width = '100%';
                    grid.style.gap = '2px';
                    grid.style.borderRadius = '16px';
                    grid.style.overflow = 'hidden';
                    grid.style.background = '#000';
                    if (videoSlides.length === 2) {
                      grid.style.gridTemplateColumns = '1fr 1fr';
                      grid.style.gridTemplateRows = '1fr';
                      grid.style.aspectRatio = '16 / 9';
                    } else if (videoSlides.length === 3) {
                      grid.style.gridTemplateColumns = '3fr 2fr';
                      grid.style.gridTemplateRows = '1fr 1fr';
                      grid.style.aspectRatio = '16 / 10';
                    } else {
                      grid.style.gridTemplateColumns = '1fr 1fr';
                      grid.style.gridTemplateRows = '1fr 1fr';
                      grid.style.aspectRatio = '1 / 1';
                    }
                    for (let i = 0; i < Math.min(videoSlides.length, 4); i++) {
                      const cell = document.createElement('div');
                      cell.style.position = 'relative';
                      cell.style.overflow = 'hidden';
                      cell.style.minWidth = '0';
                      cell.style.minHeight = '0';
                      cell.style.width = '100%';
                      cell.style.height = '100%';
                      cell.style.background = '#000';
                      const slide = videoSlides[i];
                      const video = slide.querySelector('video');
                      if (video) {
                        const clone = video.cloneNode(true);
                        clone.removeAttribute('style');
                        clone.className = '';
                        clone.style.width = '100%';
                        clone.style.height = '100%';
                        clone.style.objectFit = 'cover';
                        clone.style.objectPosition = 'top';
                        clone.style.display = 'block';
                        clone.muted = true;
                        clone.defaultMuted = true;
                        clone.playsInline = true;
                        clone.controls = false;
                        clone.autoplay = false;
                        clone.currentTime = video.currentTime || 0;
                        cell.appendChild(clone);
                      } else {
                        const inner = slide.cloneNode(true);
                        inner.style.width = '100%';
                        inner.style.height = '100%';
                        cell.appendChild(inner);
                      }
                      if (videoSlides.length === 3 && i === 0) {
                        cell.style.gridRow = '1 / span 2';
                      }
                      grid.appendChild(cell);
                    }
                    carousel.replaceWith(grid);
                  } else if (videoSlides.length === 1) {
                    carousel.style.display = 'block';
                    carousel.style.overflow = 'hidden';
                    carousel.style.width = '100%';
                    carousel.style.borderRadius = '16px';
                    const slide = videoSlides[0];
                    slide.style.width = '100%';
                    slide.style.maxWidth = '100%';
                    slide.style.flexShrink = '0';
                  }
                  continue;
                }

                if (images.length === 1) {
                  carousel.style.display = 'block';
                  carousel.style.overflow = 'hidden';
                  carousel.style.width = '100%';
                  carousel.style.borderRadius = '16px';
                  for (const slide of slides) {
                    slide.style.width = '100%';
                    slide.style.maxWidth = '100%';
                    slide.style.flexShrink = '0';
                  }
                  const img = images[0];
                  img.style.width = '100%';
                  img.style.height = 'auto';
                  img.style.objectFit = 'cover';
                  img.style.display = 'block';
                  img.style.borderRadius = '16px';
                  continue;
                }

                const grid = document.createElement('div');
                grid.setAttribute(GRID_ATTR, 'true');
                grid.style.display = 'grid';
                grid.style.width = '100%';
                grid.style.gap = '2px';
                grid.style.borderRadius = '16px';
                grid.style.overflow = 'hidden';
                grid.style.background = '#000';

                if (images.length === 2) {
                  grid.style.gridTemplateColumns = '1fr 1fr';
                  grid.style.gridTemplateRows = '1fr';
                  for (const img of images.slice(0, 2)) {
                    grid.appendChild(buildGridCell(img));
                  }
                } else if (images.length === 3) {
                  grid.style.gridTemplateColumns = '1fr 1fr';
                  grid.style.gridTemplateRows = '1fr 1fr';
                  const cells = images.slice(0, 3).map((img) => buildGridCell(img));
                  cells[0].style.gridRow = '1 / span 2';
                  cells[0].style.gridColumn = '1';
                  cells[1].style.gridRow = '1';
                  cells[1].style.gridColumn = '2';
                  cells[2].style.gridRow = '2';
                  cells[2].style.gridColumn = '2';
                  for (const cell of cells) {
                    grid.appendChild(cell);
                  }
                } else {
                  grid.style.gridTemplateColumns = '1fr 1fr';
                  grid.style.gridTemplateRows = '1fr 1fr';
                  for (const img of images.slice(0, 4)) {
                    grid.appendChild(buildGridCell(img));
                  }
                }

                mountPhotoGrid(carousel, grid, images.length);
              }

              const tweetRoot = root.matches('article')
                ? root
                : root.querySelector('article') || root;
              const photoRoots = [...root.querySelectorAll('[data-testid="tweetPhoto"]')].filter(
                (node) => node instanceof HTMLElement,
              );
              const multiPhoto = photoRoots.length > 1;
              if (tweetRoot instanceof HTMLElement) {
                tweetRoot.toggleAttribute('data-resource-snapshot-single-photo', !multiPhoto && photoRoots.length === 1);
                tweetRoot.toggleAttribute('data-resource-snapshot-multi-photo', multiPhoto);
              }

              const constrainMediaTree = (mediaEl, { fillCover = false } = {}) => {
                if (!(mediaEl instanceof HTMLElement)) {
                  return;
                }
                mediaEl.style.setProperty('max-width', '100%', 'important');
                mediaEl.style.setProperty('width', '100%', 'important');
                mediaEl.style.setProperty('display', 'block', 'important');
                if (fillCover) {
                  // Multi-photo / rebuilt grids: keep center cover crop like X.
                  mediaEl.style.setProperty('height', '100%', 'important');
                  mediaEl.style.setProperty('object-fit', 'cover', 'important');
                  mediaEl.style.setProperty('object-position', 'center center', 'important');
                } else {
                  mediaEl.style.setProperty('height', 'auto', 'important');
                  mediaEl.style.setProperty('object-fit', 'contain', 'important');
                }

                let node = mediaEl.parentElement;
                while (node instanceof HTMLElement && node !== root) {
                  node.style.minWidth = '0';
                  node.style.maxWidth = '100%';
                  node.style.overflow = 'hidden';
                  if (node.getAttribute('data-testid') === 'tweetPhoto' ||
                      node.getAttribute('data-testid') === 'videoComponent' ||
                      node.getAttribute('data-testid') === 'videoPlayer') {
                    if (!multiPhoto || node.getAttribute('data-testid') !== 'tweetPhoto') {
                      node.style.width = '100%';
                    }
                    node.style.borderRadius = multiPhoto ? '0' : '16px';
                    break;
                  }
                  node = node.parentElement;
                }
              };

              for (const img of root.querySelectorAll('img')) {
                if (!isMediaImage(img)) {
                  continue;
                }
                // Custom rebuilt grids already use cover + height 100% via CSS.
                // Forcing height:auto here clips portrait cells to the top edge.
                if (img.closest(`[${GRID_ATTR}]`)) {
                  continue;
                }
                const inTweetPhoto = Boolean(img.closest('[data-testid="tweetPhoto"]'));
                if (multiPhoto && inTweetPhoto) {
                  constrainMediaTree(img, { fillCover: true });
                  continue;
                }
                if (inTweetPhoto && photoRoots.length === 1) {
                  constrainMediaTree(img, { fillCover: false });
                  continue;
                }
                // Guest / markup without tweetPhoto: keep the on-page frame.
                // Forcing height:auto blows a constrained portrait into full
                // natural height and leaves empty chrome under the tweet.
                img.style.setProperty('max-width', '100%', 'important');
              }

              for (const photoRoot of photoRoots) {
                photoRoot.style.overflow = 'hidden';
                photoRoot.style.maxWidth = '100%';
                photoRoot.style.minWidth = '0';
                if (!multiPhoto) {
                  photoRoot.style.borderRadius = '16px';
                  photoRoot.style.width = '100%';
                }
              }

              // Keep the outer multi-photo frame rounded like X.
              if (multiPhoto) {
                const firstPhoto = photoRoots[0];
                let frame = firstPhoto?.parentElement;
                while (frame instanceof HTMLElement && frame !== root) {
                  const photosInFrame = frame.querySelectorAll('[data-testid="tweetPhoto"]').length;
                  if (photosInFrame >= photoRoots.length) {
                    frame.style.borderRadius = '16px';
                    frame.style.overflow = 'hidden';
                    frame.style.maxWidth = '100%';
                    break;
                  }
                  frame = frame.parentElement;
                }
              }
            }
            """
        )
    except Exception:
        pass


def _prepare_tweet_for_screenshot(tweet_card) -> None:
    _prepare_tweet_media_for_screenshot(tweet_card)
    try:
        tweet_card.evaluate(
            """
            (root) => {
              const isVisible = (node) => {
                if (!(node instanceof Element)) {
                  return false;
                }
                const style = window.getComputedStyle(node);
                if (!style || style.display === 'none' || style.visibility === 'hidden') {
                  return false;
                }
                const rect = node.getBoundingClientRect();
                return rect.width >= 8 && rect.height >= 8;
              };

              const removeNode = (node) => {
                if (node instanceof Element) {
                  node.remove();
                }
              };

              const replyRestrictionPattern =
                /only some accounts can reply|who can reply|仅限部分|只有部分.*回复|回复受限|このポストに返信|返信できるの/i;

              // Guest detail pages put the lock banner in <output>; remove it and
              // any similarly worded chrome so it cannot pad the screenshot.
              root.querySelectorAll('output').forEach(removeNode);
              for (const node of [...root.querySelectorAll('div, aside, section, span')]) {
                if (!(node instanceof HTMLElement)) {
                  continue;
                }
                const text = (node.innerText || node.textContent || '').trim();
                if (!text || text.length > 120) {
                  continue;
                }
                if (replyRestrictionPattern.test(text)) {
                  const removable =
                    node.closest('output') ||
                    (node.children.length <= 3 ? node : null) ||
                    node;
                  removeNode(removable);
                }
              }

              const replySelectors = [
                '[data-testid="logged_out_read_replies_pivot"]',
                '[data-testid="inline_reply_offscreen"]',
              ];
              for (const selector of replySelectors) {
                root.querySelectorAll(selector).forEach(removeNode);
              }

              root.querySelectorAll('a, button, [role="button"], span').forEach((node) => {
                const text = (node.textContent || '').trim();
                if (!text || text.length > 80) {
                  return;
                }
                if (
                  /^(Read|See)\\s+\\d+[\\d,]*\\s+repl/i.test(text) ||
                  /^阅读\\s*\\d+/.test(text) ||
                  /^\\d+\\s*条回复/.test(text)
                ) {
                  removeNode(node);
                }
              });

              const tweetRoot = root.matches('article') ? root : root.querySelector('article') || root;
              tweetRoot.style.width = '100%';
              tweetRoot.style.maxWidth = '598px';
              tweetRoot.style.marginLeft = 'auto';
              tweetRoot.style.marginRight = 'auto';

              const engagementSelector = [
                '[data-testid="reply"]',
                '[data-testid="retweet"]',
                '[data-testid="like"]',
                '[data-testid="bookmark"]',
                '[data-testid="share"]',
              ].join(', ');
              const engagementAria =
                /^(reply|repost|retweet|like|bookmark|share|回复|转推|喜欢|收藏|分享)\\b/i;

              const findEngagementButtons = (scope) =>
                [...scope.querySelectorAll('button, a, [role="button"]')].filter((node) => {
                  if (!isVisible(node)) {
                    return false;
                  }
                  if (node.matches(engagementSelector) || node.closest(engagementSelector)) {
                    return true;
                  }
                  const label = (node.getAttribute('aria-label') || '').trim();
                  return engagementAria.test(label);
                });

              const rootRect = tweetRoot.getBoundingClientRect();
              const actionGroups = [...tweetRoot.querySelectorAll('[role="group"]')]
                .filter(isVisible)
                .map((node) => {
                  const rect = node.getBoundingClientRect();
                  return {
                    node,
                    rect,
                    text: (node.innerText || '').trim(),
                  };
                })
                .filter((entry) => {
                  if (entry.rect.width < 180 || entry.rect.height < 12) {
                    return false;
                  }
                  if (
                    entry.node.closest(
                      '[data-testid="videoComponent"], [data-testid="videoPlayer"]',
                    )
                  ) {
                    return false;
                  }
                  const hasEngagementButton = Boolean(
                    entry.node.querySelector(engagementSelector) ||
                      findEngagementButtons(entry.node).length > 0,
                  );
                  const looksLikeEngagement = /reply|repost|retweet|like|bookmark|share|回复|转推|喜欢|收藏|分享/i.test(
                    entry.text,
                  );
                  return hasEngagementButton || looksLikeEngagement;
                });
              let actionBar = actionGroups.sort(
                (a, b) => b.rect.bottom - a.rect.bottom,
              )[0]?.node;

              // Guest UI often has no role=group / data-testid; fall back to the
              // shared parent of aria-labelled engagement buttons.
              if (!actionBar) {
                const buttons = findEngagementButtons(tweetRoot);
                if (buttons.length >= 2) {
                  let common = buttons[0].parentElement;
                  while (
                    common &&
                    common !== tweetRoot &&
                    !buttons.every((button) => common.contains(button))
                  ) {
                    common = common.parentElement;
                  }
                  if (common && common !== tweetRoot) {
                    // Prefer a reasonably wide row rather than a tiny icon wrapper.
                    let candidate = common;
                    while (candidate && candidate !== tweetRoot) {
                      const rect = candidate.getBoundingClientRect();
                      if (rect.width >= Math.min(220, rootRect.width * 0.55)) {
                        actionBar = candidate;
                        break;
                      }
                      candidate = candidate.parentElement;
                    }
                    actionBar = actionBar || common;
                  }
                }
              }

              if (actionBar) {
                const containsFooterMetadata = (node) => {
                  if (!(node instanceof HTMLElement)) {
                    return false;
                  }
                  const text = (node.innerText || '').trim();
                  return (
                    /(views|查看|次观看)/i.test(text) &&
                    /(\\d{1,2}:\\d{2}|年|AM|PM|·)/i.test(text)
                  );
                };

                const removeFollowing = (container, pivot) => {
                  let seen = false;
                  for (const child of [...container.children]) {
                    if (seen) {
                      if (containsFooterMetadata(child)) {
                        continue;
                      }
                      removeNode(child);
                      continue;
                    }
                    if (child === pivot || child.contains(pivot)) {
                      seen = true;
                    }
                  }
                };

                let pivot = actionBar;
                while (pivot && pivot !== tweetRoot) {
                  const parent = pivot.parentElement;
                  if (!parent) {
                    break;
                  }
                  removeFollowing(parent, pivot);
                  if (parent === tweetRoot) {
                    break;
                  }
                  pivot = parent;
                }
              }

              // Final sweep in case the lock banner remounted or sat outside the
              // engagement-bar sibling walk.
              root.querySelectorAll('output').forEach(removeNode);
              for (const node of [...root.querySelectorAll('div, aside, section')]) {
                if (!(node instanceof HTMLElement)) {
                  continue;
                }
                const text = (node.innerText || '').trim();
                if (text && text.length <= 120 && replyRestrictionPattern.test(text)) {
                  removeNode(node);
                }
              }

              for (const node of root.querySelectorAll('div, aside, section')) {
                if (!(node instanceof HTMLElement)) {
                  continue;
                }
                const text = (node.innerText || '').trim();
                if (!text || text.length > 220) {
                  continue;
                }
                if (
                  /don't miss what's happening|sign up now to get your own|people on x are the first/i.test(
                    text,
                  )
                ) {
                  removeNode(node);
                }
              }

              const hideVideoControlChrome = (video) => {
                const findVideoPlayerRoot = (videoNode, cardRoot) => {
                  const explicit =
                    videoNode.closest('[data-testid="videoComponent"]') ||
                    videoNode.closest('[data-testid="videoPlayer"]');
                  if (explicit instanceof HTMLElement) {
                    return explicit;
                  }

                  const videoRect = videoNode.getBoundingClientRect();
                  let node = videoNode.parentElement;
                  while (node instanceof HTMLElement && node !== cardRoot) {
                    const rect = node.getBoundingClientRect();
                    if (
                      rect.width <= Math.max(videoRect.width * 1.8, videoRect.width + 96) &&
                      rect.height <= Math.max(videoRect.height * 2.5, videoRect.height + 96)
                    ) {
                      return node;
                    }
                    node = node.parentElement;
                  }

                  return videoNode.parentElement instanceof HTMLElement ? videoNode.parentElement : videoNode;
                };

                const playerRoot = findVideoPlayerRoot(video, tweetRoot);
                if (!(playerRoot instanceof HTMLElement) || playerRoot === tweetRoot) {
                  return;
                }
                if (/(views|查看|次观看)/i.test(playerRoot.innerText || '')) {
                  return;
                }

                video.controls = false;
                playerRoot.querySelectorAll(
                  'button,[role="button"],[role="progressbar"],[role="slider"],input[type="range"],svg,img',
                ).forEach((node) => {
                  if (!(node instanceof HTMLElement) || node === video || video.contains(node)) {
                    return;
                  }
                  node.style.setProperty('display', 'none', 'important');
                });

                const rootRect = playerRoot.getBoundingClientRect();
                for (const node of playerRoot.querySelectorAll('div')) {
                  if (!(node instanceof HTMLElement) || node === video || video.contains(node) || node.contains(video)) {
                    continue;
                  }
                  const rect = node.getBoundingClientRect();
                  if (rect.width < rootRect.width * 0.45 || rect.height < 4 || rect.height > 96) {
                    continue;
                  }
                  if (rect.bottom >= rootRect.bottom - 8 && rect.top >= rootRect.bottom - 100) {
                    node.style.setProperty('display', 'none', 'important');
                    node.style.setProperty('background', 'transparent', 'important');
                  }
                }

                const videoRect = video.getBoundingClientRect();
                if (videoRect.height > 0) {
                  const topOffset = Math.max(0, videoRect.top - rootRect.top);
                  const targetHeight = Math.ceil(topOffset + videoRect.height);
                  playerRoot.style.height = `${targetHeight}px`;
                  playerRoot.style.maxHeight = `${targetHeight}px`;
                  playerRoot.style.overflow = 'hidden';
                  playerRoot.style.background = 'transparent';
                  playerRoot.style.paddingBottom = '0';
                }
              };

              const trimVideoContainer = (video) => {
                const rect = video.getBoundingClientRect();
                if (rect.width < 1 || rect.height < 1) {
                  return;
                }

                let sourceWidth = video.videoWidth;
                let sourceHeight = video.videoHeight;
                if (!sourceWidth || !sourceHeight) {
                  sourceWidth = rect.width;
                  sourceHeight = rect.height;
                }
                if (!sourceWidth || !sourceHeight) {
                  return;
                }

                const aspect = sourceWidth / sourceHeight;
                const targetWidth = Math.min(rect.height * aspect, rect.width);
                const playerRoot =
                  video.closest('[data-testid="videoComponent"]') ||
                  video.closest('[data-testid="videoPlayer"]') ||
                  video.parentElement;
                if (!(playerRoot instanceof HTMLElement)) {
                  return;
                }

                const chain = [];
                let current = video;
                while (current && current !== playerRoot) {
                  chain.push(current);
                  current = current.parentElement;
                }
                chain.push(playerRoot);

                for (const node of chain) {
                  if (!(node instanceof HTMLElement)) {
                    continue;
                  }
                  node.style.width = `${targetWidth}px`;
                  node.style.maxWidth = `${targetWidth}px`;
                  node.style.minWidth = '0';
                  node.style.marginLeft = 'auto';
                  node.style.marginRight = 'auto';
                  node.style.background = 'transparent';
                  node.style.overflow = 'hidden';
                }

                video.style.width = '100%';
                video.style.height = 'auto';
                video.style.objectFit = 'contain';
                video.style.background = 'transparent';
              };

              const stabilizeFooterLayout = () => {
                for (const row of tweetRoot.querySelectorAll('[role="group"]')) {
                  if (!(row instanceof HTMLElement)) {
                    continue;
                  }
                  const rect = row.getBoundingClientRect();
                  if (rect.width < 80 || rect.height > 96) {
                    continue;
                  }
                  row.style.width = '100%';
                  row.style.maxWidth = '100%';
                  row.style.minWidth = '0';
                  row.style.flex = '1 1 auto';
                }

                for (const row of tweetRoot.querySelectorAll('div')) {
                  if (!(row instanceof HTMLElement)) {
                    continue;
                  }
                  const text = (row.innerText || '').trim();
                  if (!/(views|查看|次观看)/i.test(text)) {
                    continue;
                  }
                  if (!/(\\d{1,2}:\\d{2}|年|AM|PM|·)/i.test(text)) {
                    continue;
                  }
                  const rect = row.getBoundingClientRect();
                  if (rect.height > 60 || rect.width < 120) {
                    continue;
                  }
                  row.style.width = '100%';
                  row.style.maxWidth = '100%';

                  for (const span of row.querySelectorAll('span')) {
                    if (!(span instanceof HTMLElement)) {
                      continue;
                    }
                    const label = (span.textContent || '').trim();
                    if (/^(views|查看|次观看)$/i.test(label)) {
                      span.style.display = 'inline';
                      span.style.marginLeft = '0.25em';
                    }
                    if (/^[\\d,.]+万?$/.test(label)) {
                      span.style.display = 'inline';
                    }
                  }
                }
              };

              for (const video of root.querySelectorAll('video')) {
                if (!isVisible(video)) {
                  continue;
                }
                hideVideoControlChrome(video);
                trimVideoContainer(video);
              }

              stabilizeFooterLayout();
            }
            """
        )
    except Exception:
        pass


def _compute_capture_clip(page, tweet_card):
    element = tweet_card.element_handle(timeout=5000)
    if element is None:
        return None

    clip_script = """
        (el) => {
          const doc = document.documentElement;
          const rootRect = el.getBoundingClientRect();
          const mediaSelector = 'img, svg, video, canvas, picture, iframe';
          const excludedSelector = [
            '[data-testid="logged_out_read_replies_pivot"]',
            '[data-testid="inline_reply_offscreen"]',
            '[data-testid="reply"]',
            '[data-testid="tweetTextarea_0"]',
            '[data-testid="inline_reply_composer"]',
            '[contenteditable="true"][role="textbox"]',
            '[role="textbox"]',
            'form[aria-label*="Reply"]',
            'form[aria-label*="reply"]',
            'form[aria-label*="回复"]',
            'form[aria-label*="回覆"]',
            'output',
          ].join(', ');

          let left = Infinity;
          let top = Infinity;
          let right = -Infinity;
          let bottom = -Infinity;

          const addRect = (rect) => {
            if (!rect || rect.width < 2 || rect.height < 2) {
              return;
            }
            left = Math.min(left, rect.left + window.scrollX);
            top = Math.min(top, rect.top + window.scrollY);
            right = Math.max(right, rect.right + window.scrollX);
            bottom = Math.max(bottom, rect.bottom + window.scrollY);
          };

          const isVisible = (node) => {
            const style = window.getComputedStyle(node);
            if (!style) {
              return false;
            }
            if (style.display === 'none' || style.visibility === 'hidden') {
              return false;
            }
            if (Number(style.opacity || '1') === 0) {
              return false;
            }
            return true;
          };

          const shouldExclude = (node) => {
            if (!node || !(node instanceof Element)) {
              return false;
            }
            return Boolean(node.closest(excludedSelector));
          };

          const textRects = () => {
            const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
            while (walker.nextNode()) {
              const textNode = walker.currentNode;
              if (!textNode.textContent || !textNode.textContent.trim()) {
                continue;
              }
              const parent = textNode.parentElement;
              if (!parent || !isVisible(parent)) {
                continue;
              }
              if (shouldExclude(parent)) {
                continue;
              }

              const range = document.createRange();
              range.selectNodeContents(textNode);
              for (const rect of range.getClientRects()) {
                addRect(rect);
              }
            }
          };

          textRects();

          const headerSelector = [
            '[data-testid="User-Name"]',
            '[data-testid="Tweet-User-Avatar"]',
            '[data-testid="UserAvatar-Container-Unknown"]',
          ].join(', ');
          for (const node of el.querySelectorAll(headerSelector)) {
            if (!isVisible(node)) {
              continue;
            }
            addRect(node.getBoundingClientRect());
          }

          const isTweetBodyText = (node) => {
            if (!(node instanceof Element)) {
              return false;
            }
            if (node.closest('[data-testid="User-Name"]')) {
              return false;
            }
            if (node.closest('[data-testid="socialContext"]')) {
              return false;
            }
            if (node.closest('[role="group"]')) {
              return false;
            }
            return true;
          };

          for (const selector of ['[data-testid="tweetText"]', 'div[dir="auto"]']) {
            for (const node of el.querySelectorAll(selector)) {
              if (!isVisible(node) || shouldExclude(node) || !isTweetBodyText(node)) {
                continue;
              }
              addRect(node.getBoundingClientRect());
            }
          }

          for (const node of el.querySelectorAll('[__TRANSLATION_ATTR__="block"]')) {
            if (!isVisible(node)) {
              continue;
            }
            addRect(node.getBoundingClientRect());
          }

          for (const node of el.querySelectorAll(mediaSelector)) {
            if (!isVisible(node)) {
              continue;
            }
            if (shouldExclude(node)) {
              continue;
            }
            if (
              node instanceof HTMLImageElement &&
              node.closest('[data-testid="Tweet-User-Avatar"], [data-testid="UserAvatar-Container-Unknown"], [data-testid="User-Name"]')
            ) {
              continue;
            }
            for (const rect of node.getClientRects()) {
              addRect(rect);
            }
          }

          const engagementSelector = [
            '[data-testid="reply"]',
            '[data-testid="retweet"]',
            '[data-testid="like"]',
            '[data-testid="bookmark"]',
            '[data-testid="share"]',
          ].join(', ');
          const engagementAria =
            /^(reply|repost|retweet|like|bookmark|share|回复|转推|喜欢|收藏|分享)\\b/i;

          const findEngagementButtons = (scope) =>
            [...scope.querySelectorAll('button, a, [role="button"]')].filter((node) => {
              if (!isVisible(node)) {
                return false;
              }
              if (node.matches(engagementSelector) || node.querySelector(engagementSelector)) {
                return true;
              }
              // Guest UI uses aria-label without data-testid.
              if (node.closest(engagementSelector)) {
                return true;
              }
              const label = (node.getAttribute('aria-label') || '').trim();
              return engagementAria.test(label);
            });

          const actionGroups = [...el.querySelectorAll('[role="group"]')]
            .filter((node) => isVisible(node))
            .map((node) => {
              const rect = node.getBoundingClientRect();
              return {
                node,
                top: rect.top + window.scrollY,
                bottom: rect.bottom + window.scrollY,
                width: rect.width,
                height: rect.height,
                text: (node.innerText || '').trim(),
                hasEngagementButton: Boolean(
                  node.querySelector(engagementSelector) ||
                    findEngagementButtons(node).length > 0,
                ),
              };
            })
            .filter((rect) => {
              if (rect.width < 180 || rect.height < 12) {
                return false;
              }
              const nearBottom = rect.bottom >= rootRect.top + rootRect.height * 0.45;
              const looksLikeEngagement = /reply|repost|retweet|like|bookmark|share|回复|转推|喜欢|收藏|分享/i.test(
                rect.text,
              );
              return rect.hasEngagementButton || nearBottom || looksLikeEngagement;
            });

          let actionBar = actionGroups.sort((a, b) => {
            if (a.hasEngagementButton !== b.hasEngagementButton) {
              return a.hasEngagementButton ? -1 : 1;
            }
            return b.bottom - a.bottom;
          })[0];

          if (!actionBar) {
            const buttons = findEngagementButtons(el);
            if (buttons.length > 0) {
              let bottom = -Infinity;
              let top = Infinity;
              let leftmost = Infinity;
              let rightmost = -Infinity;
              for (const button of buttons) {
                const rect = button.getBoundingClientRect();
                bottom = Math.max(bottom, rect.bottom + window.scrollY);
                top = Math.min(top, rect.top + window.scrollY);
                leftmost = Math.min(leftmost, rect.left + window.scrollX);
                rightmost = Math.max(rightmost, rect.right + window.scrollX);
              }
              if (Number.isFinite(bottom)) {
                actionBar = {
                  top,
                  bottom,
                  width: Math.max(1, rightmost - leftmost),
                  height: Math.max(1, bottom - top),
                  text: '',
                  hasEngagementButton: true,
                };
              }
            }
          }

          const rootX = rootRect.left + window.scrollX;
          const rootY = rootRect.top + window.scrollY;
          const rootRight = rootX + rootRect.width;
          const rootBottom = rootY + rootRect.height;

          if (!Number.isFinite(left)) {
            left = rootX;
            top = rootY;
            right = rootRight;
            bottom = rootBottom;
          } else {
            top = Math.min(top, rootY);
            // Prefer measured content over the full article box. Conversation
            // chrome / trailing spacers can inflate rootBottom past the tweet.
            if (!actionBar) {
              bottom = Math.max(bottom, rootBottom);
            }
          }

          left = rootX;
          right = rootRight;

          const padding = 12;
          const bottomPadding = actionBar ? 8 : padding;
          const x = Math.max(0, Math.floor(left - padding));
          const y = Math.max(0, Math.floor(top - padding));
          const maxRight = Math.max(doc.scrollWidth, right + padding);
          // Hard-stop at the engagement row so reply-lock banners / replies
          // under the detail tweet never enter the capture rectangle.
          const contentBottom = actionBar
            ? actionBar.bottom
            : Math.min(bottom, rootBottom);
          const maxBottom = Math.max(doc.scrollHeight, contentBottom + bottomPadding);
          const width = Math.max(1, Math.ceil(Math.min(maxRight, right + padding) - x));
          let height = Math.max(1, Math.ceil(Math.min(maxBottom, contentBottom + bottomPadding) - y));

          // Guard against flex/min-content blowups from large orig/srcset images.
          // A detail card with quote media should rarely exceed ~4x column width.
          const maxHeight = Math.max(Math.ceil(rootRect.width * 4.5), 2400);
          if (height > maxHeight) {
            height = maxHeight;
          }

          return { x, y, width, height };
        }
        """.replace("__TRANSLATION_ATTR__", TRANSLATION_ATTR)

    return page.evaluate(
        clip_script,
        arg=element,
    )


def _ensure_viewport_can_fit_clip(page, clip: dict[str, int] | None) -> bool:
    if not clip:
        return False

    viewport = page.viewport_size or {
        "width": DEFAULT_VIEWPORT_WIDTH,
        "height": DEFAULT_VIEWPORT_HEIGHT,
    }
    required_width = max(int(clip["width"]) + CAPTURE_VIEWPORT_MARGIN, min(viewport["width"], 760))
    required_height = max(int(clip["height"]) + CAPTURE_VIEWPORT_MARGIN, DEFAULT_VIEWPORT_HEIGHT)

    if required_width <= viewport["width"] and required_height <= viewport["height"]:
        return False

    page.set_viewport_size(
        {
            "width": required_width,
            "height": required_height,
        }
    )
    page.wait_for_timeout(300)
    return True


def _capture_detail_snapshot(
    page,
    tweet_card,
    path: Path,
    *,
    tweet_id: str | None = None,
) -> None:
    _hide_non_primary_columns(page, tweet_id)
    _scroll_tweet_into_view(page, tweet_card, guest_mode=True)

    clip = _compute_capture_clip(page, tweet_card)
    if _ensure_viewport_can_fit_clip(page, clip):
        _wait_for_tweet_assets(page, tweet_card)
        page.wait_for_timeout(200)
        _hide_non_primary_columns(page, tweet_id)
        _scroll_tweet_into_view(page, tweet_card, guest_mode=True)
        clip = _compute_capture_clip(page, tweet_card)
        if _ensure_viewport_can_fit_clip(page, clip):
            page.wait_for_timeout(200)
            _hide_non_primary_columns(page, tweet_id)
            _scroll_tweet_into_view(page, tweet_card, guest_mode=True)
            clip = _compute_capture_clip(page, tweet_card)

    # Final pass: replies can remount after viewport/layout changes.
    _hide_non_primary_columns(page, tweet_id)
    clip = _compute_capture_clip(page, tweet_card) or clip

    if clip:
        page.screenshot(
            path=str(path),
            animations="disabled",
            clip=clip,
        )
        return

    tweet_card.screenshot(
        path=str(path),
        animations="disabled",
    )


def _build_output_name(detail_url: str, output_dir: Path) -> str:
    parsed = urlparse(detail_url)
    host = (parsed.netloc or "x.com").lower()
    path = parsed.path.strip("/")
    raw_name = "_".join(part for part in [host, path.replace("/", "_")] if part)
    safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", raw_name).strip("._-") or "tweet"

    candidate = f"{safe_name}.png"
    sequence = 2
    while (output_dir / candidate).exists():
        candidate = f"{safe_name}_{sequence}.png"
        sequence += 1

    return candidate


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
            headless=True,
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


def _load_tweet_card(
    page,
    normalized_url: str,
    screen_name: str,
    tweet_id: str,
    *,
    dark_mode: bool,
    wait_timeout_ms: int,
    guest_mode: bool = False,
):
    last_error: Exception | None = None

    for candidate_url, mode in _candidate_urls(normalized_url, screen_name, tweet_id):
        try:
            # X detail pages often stay on readyState=interactive with open media
            # sockets, so waiting for "domcontentloaded" can hang until timeout
            # even after the tweet article is already in the DOM.
            page.goto(candidate_url, wait_until="commit")
            try:
                page.wait_for_load_state("domcontentloaded", timeout=5000)
            except PlaywrightTimeoutError:
                pass
            if guest_mode:
                page.wait_for_timeout(GUEST_PAGE_SETTLE_MS)
            else:
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except PlaywrightTimeoutError:
                    pass
                _dismiss_common_overlays(page)

            tweet_card = _wait_for_tweet_card(page, tweet_id, wait_timeout_ms)
            if tweet_card is None:
                raise RuntimeError("页面里没有找到可截图的推文主体")

            _expand_tweet_text(tweet_card)
            page.wait_for_timeout(250)
            page.add_style_tag(content=_detail_capture_css(dark_mode))
            _hide_non_primary_columns(page, tweet_id)
            if not guest_mode:
                _dismiss_common_overlays(page)
            return tweet_card, candidate_url, mode
        except Exception as exc:
            last_error = exc
            continue

    detail = (
        "可能是链接无效、推文已删除，或该推文需要先登录 X 才能查看。"
        " 如果需要登录，请勾选页面里的“显示浏览器”后重新截图。"
    )
    if last_error:
        raise RuntimeError(detail) from last_error
    raise RuntimeError(detail)


def preview_tweet_translations(
    url: str,
    profile_dir: Path | str,
    *,
    headless: bool = True,
    dark_mode: bool = True,
) -> TranslationPreviewResult:
    normalized_url = _normalize_input_url(url)
    screen_name, tweet_id = _extract_parts(normalized_url)

    browser_profile = Path(profile_dir)
    browser_profile.mkdir(parents=True, exist_ok=True)
    guest_mode = headless
    wait_timeout_ms = 90000 if not headless else 30000

    with sync_playwright() as playwright:
        session = _open_capture_session(
            playwright,
            browser_profile,
            headless=headless,
            dark_mode=dark_mode,
            wait_timeout_ms=wait_timeout_ms,
            guest_mode=guest_mode,
        )
        page = session.page
        try:
            tweet_card, used_url, capture_mode = _load_tweet_card(
                page,
                normalized_url,
                screen_name,
                tweet_id,
                dark_mode=dark_mode,
                wait_timeout_ms=wait_timeout_ms,
                guest_mode=guest_mode,
            )
            _wait_for_tweet_assets(page, tweet_card)

            text_blocks = _collect_translation_text_blocks(tweet_card, used_url or normalized_url)
            items = tuple(
                TranslationPreviewItem(
                    index=int(item["index"]),
                    label=_translation_label_for_index(int(item["index"])),
                    original_text=str(item["text"]),
                    suggested_translation=str(item["translation"]),
                )
                for item in _build_translation_items(text_blocks)
            )
        finally:
            session.close()

    return TranslationPreviewResult(
        items=items,
        used_url=used_url,
        capture_mode=capture_mode,
        tweet_id=tweet_id,
    )


def capture_tweet_page(
    url: str,
    output_dir: Path | str,
    profile_dir: Path | str,
    *,
    headless: bool = True,
    dark_mode: bool = True,
    video_timestamp_seconds: float | None = None,
    video_frame_schedule: dict[str, dict[str, float | None]] | None = None,
    translate_body: bool = False,
    custom_translation: str | None = None,
    translation_overrides: dict[int, str] | None = None,
) -> CaptureResult:
    normalized_url = _normalize_input_url(url)
    screen_name, tweet_id = _extract_parts(normalized_url)

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    browser_profile = Path(profile_dir)
    browser_profile.mkdir(parents=True, exist_ok=True)

    file_name = _build_output_name(normalized_url, output_path)
    saved_to = output_path / file_name
    used_url = ""
    capture_mode = ""
    video_frame_seconds = None
    video_frames: tuple[VideoFrameInfo, ...] = ()
    schedule = video_frame_schedule
    if schedule is None:
        schedule = {"byIndex": {}, "named": {}}
        if video_timestamp_seconds is not None:
            schedule["byIndex"]["0"] = video_timestamp_seconds
    guest_mode = headless
    wait_timeout_ms = 90000 if not headless else 30000

    with sync_playwright() as playwright:
        session = _open_capture_session(
            playwright,
            browser_profile,
            headless=headless,
            dark_mode=dark_mode,
            wait_timeout_ms=wait_timeout_ms,
            guest_mode=guest_mode,
        )
        page = session.page

        try:
            # Soft-ceiling HLS may still include a top rung Chromium cannot
            # decode (e.g. portrait 1080 avc1.640032). On failure, demote the
            # ceiling and reload so the player re-fetches a lower multi-variant
            # master (720p+ rungs) instead of staying stuck on Format error.
            max_hls_attempts = 1 + len(HLS_DEMOTE_STEPS)
            tweet_card = None
            for hls_attempt in range(max_hls_attempts):
                tweet_card, used_url, capture_mode = _load_tweet_card(
                    page,
                    normalized_url,
                    screen_name,
                    tweet_id,
                    dark_mode=dark_mode,
                    wait_timeout_ms=wait_timeout_ms,
                    guest_mode=guest_mode,
                )
                _wait_for_tweet_assets(page, tweet_card)
                _expand_tweet_text(tweet_card)
                page.wait_for_timeout(200)
                if translate_body:
                    _inject_chinese_translations(
                        tweet_card,
                        custom_translation=custom_translation,
                        translation_overrides=translation_overrides,
                        status_url=used_url or normalized_url,
                    )
                    _remove_native_translation_ui(tweet_card)
                _scroll_tweet_into_view(page, tweet_card, guest_mode=guest_mode)
                page.wait_for_timeout(250)
                video_frames = _prepare_video_frames(tweet_card, schedule)
                if video_frames:
                    video_frame_seconds = video_frames[0].seconds
                    break
                if not _tweet_video_undecodable(tweet_card):
                    break
                if not _demote_hls_quality_gate(page.context):
                    break
                if hls_attempt + 1 >= max_hls_attempts:
                    break

            _prepare_tweet_for_screenshot(tweet_card)
            page.wait_for_timeout(200)
            _capture_detail_snapshot(
                page,
                tweet_card,
                saved_to,
                tweet_id=tweet_id,
            )
        finally:
            session.close()

    return CaptureResult(
        file_name=file_name,
        file_path=saved_to,
        preview_url=f"/screenshots/{file_name}",
        capture_mode=capture_mode,
        used_url=used_url,
        tweet_id=tweet_id,
        video_frame_seconds=video_frame_seconds,
        video_frames=video_frames,
    )
