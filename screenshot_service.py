#!/usr/bin/env python3
"""Capture a screenshot for a single X/Twitter status URL."""

from __future__ import annotations

from dataclasses import dataclass
from html import unescape
import json
from pathlib import Path
import re
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


TWEET_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:x\.com|twitter\.com|mobile\.twitter\.com)/"
    r"(?P<screen_name>[^/?#]+)/status/(?P<tweet_id>\d+)",
    re.IGNORECASE,
)
TRANSLATION_API_URL = "https://api.mymemory.translated.net/get"
TRANSLATION_ATTR = "data-resource-snapshot-translation"


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
[data-testid="tweetTextarea_0"],
[data-testid="inline_reply_composer"] {{
  display: none !important;
}}

main[role="main"] {{
  display: block !important;
  background: {background} !important;
}}

[data-testid="primaryColumn"] {{
  width: min(760px, calc(100vw - 32px)) !important;
  max-width: none !important;
  margin: 0 auto !important;
  background: {background} !important;
}}

article[data-testid="tweet"],
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


def _embed_capture_css(dark_mode: bool) -> str:
    background = "#15202b" if dark_mode else "#ffffff"
    return f"""
html, body {{
  margin: 0 !important;
  background: {background} !important;
  color-scheme: {"dark" if dark_mode else "light"} !important;
}}
{_translation_capture_css(dark_mode)}
"""


@dataclass(frozen=True)
class CaptureResult:
    file_name: str
    file_path: Path
    preview_url: str
    capture_mode: str
    used_url: str
    tweet_id: str
    video_frame_seconds: float | None


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


def _candidate_urls(original_url: str, screen_name: str, tweet_id: str) -> list[tuple[str, str]]:
    parsed = urlparse(original_url)
    original_host = parsed.netloc.lower() or "x.com"

    detail_path = f"/{screen_name}/status/{tweet_id}"
    urls = [
        (f"https://{original_host}{detail_path}", "detail_page"),
        (f"https://x.com/i/status/{tweet_id}", "detail_page"),
        (f"https://twitter.com/i/status/{tweet_id}", "detail_page"),
        (f"https://platform.twitter.com/embed/Tweet.html?id={tweet_id}", "embed_card"),
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
          const selectors = [
            '[role="dialog"]',
            '[data-testid="sheetDialog"]',
            '[data-testid="BottomBar"]',
            '[data-testid="DMDrawer"]'
          ];
          for (const selector of selectors) {
            document.querySelectorAll(selector).forEach((node) => node.remove());
          }
          document.documentElement.style.scrollBehavior = 'auto';
          document.body.style.overflow = 'auto';
        }
        """
    )


def _wait_for_tweet_card(page, tweet_id: str, mode: str, timeout_ms: int):
    locators = []

    if mode == "detail_page":
        permalink = page.locator(
            ",".join(
                [
                    f"a[href*='/status/{tweet_id}']",
                    f"a[href*='/i/web/status/{tweet_id}']",
                    f"a[href$='/{tweet_id}']",
                ]
            )
        )
        locators.extend(
            [
                page.locator("article[data-testid='tweet']").filter(has=permalink).first,
                page.locator("article[data-testid='tweet']").first,
                page.locator("[data-testid='cellInnerDiv']").filter(has=permalink).locator("article[data-testid='tweet']").first,
                page.locator("[data-testid='cellInnerDiv']").first,
            ]
        )
    else:
        locators.extend(
            [
                page.locator("article").first,
                page.locator("main article").first,
            ]
        )

    for locator in locators:
        try:
            locator.wait_for(state="visible", timeout=timeout_ms)
            return locator
        except PlaywrightTimeoutError:
            continue
    return None


def _scroll_tweet_into_view(page, tweet_card) -> None:
    tweet_card.scroll_into_view_if_needed(timeout=10000)
    page.wait_for_timeout(500)

    box = tweet_card.bounding_box()
    if not box:
        return

    target_top = max(int(box["y"] - 120), 0)
    page.evaluate("(top) => window.scrollTo(0, top)", target_top)
    page.wait_for_timeout(700)


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


def _extract_translatable_text_blocks(tweet_card) -> list[dict[str, str | int | None]]:
    try:
        blocks = tweet_card.evaluate(
            """
            (root) => {
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
                return rect.width >= 8 && rect.height >= 8;
              };

              return [...root.querySelectorAll('[data-testid="tweetText"]')]
                .filter((node) => isVisible(node))
                .map((node, index) => ({
                  index,
                  text: (node.innerText || '').trim(),
                  lang: node.getAttribute('lang') || node.querySelector('[lang]')?.getAttribute('lang') || '',
                }))
                .filter((item) => item.text);
            }
            """
        )
    except Exception:
        return []

    if not isinstance(blocks, list):
        return []
    return blocks


def _translate_text_to_chinese(text: str, source_lang: str | None) -> str | None:
    normalized_text = (text or "").strip()
    normalized_lang = _normalize_translation_lang(source_lang)
    if not normalized_text or not normalized_lang or normalized_lang.startswith("zh"):
        return None

    query = urlencode({"q": normalized_text, "langpair": f"{normalized_lang}|zh-CN"})
    request = Request(
        f"{TRANSLATION_API_URL}?{query}",
        headers={
            "User-Agent": "resource-snapshot/1.0",
            "Accept": "application/json",
        },
    )

    try:
        with urlopen(request, timeout=12) as response:
            payload = json.load(response)
    except Exception:
        return None

    if payload.get("responseStatus") != 200:
        return None

    translated = unescape(str(payload.get("responseData", {}).get("translatedText") or "")).strip()
    if not translated:
        return None
    if translated.casefold() == normalized_text.casefold():
        return None
    return translated


def _inject_chinese_translations(tweet_card) -> int:
    text_blocks = _extract_translatable_text_blocks(tweet_card)
    if not text_blocks:
        return 0

    cache: dict[tuple[str, str], str | None] = {}
    items: list[dict[str, str | int]] = []
    for block in text_blocks:
        text = str(block.get("text") or "").strip()
        lang = _normalize_translation_lang(block.get("lang"))
        if not text or not lang or lang.startswith("zh"):
            continue

        cache_key = (text, lang)
        if cache_key not in cache:
            cache[cache_key] = _translate_text_to_chinese(text, lang)

        translation = cache[cache_key]
        if not translation:
            continue

        items.append(
            {
                "index": int(block["index"]),
                "translation": translation,
            }
        )

    if not items:
        return 0

    try:
        inserted = tweet_card.evaluate(
            f"""
            (root, entries) => {{
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

              root.querySelectorAll('[{TRANSLATION_ATTR}="block"]').forEach((node) => node.remove());
              const blocks = [...root.querySelectorAll('[data-testid="tweetText"]')].filter((node) => isVisible(node));
              let count = 0;

              for (const entry of entries) {{
                const anchor = blocks[entry.index];
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
                '重试',
              ]);
              const blockTexts = ['无法获取翻译'];

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


def _prepare_video_frame(tweet_card, target_seconds: float | None) -> float | None:
    try:
        result = tweet_card.evaluate(
            """
            async (root, targetSeconds) => {
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

              const videos = [...root.querySelectorAll('video')]
                .filter((video) => isVisible(video))
                .map((video) => {
                  const rect = video.getBoundingClientRect();
                  return { video, area: rect.width * rect.height };
                })
                .sort((a, b) => b.area - a.area);

              if (videos.length === 0) {
                return null;
              }

              const video = videos[0].video;
              video.muted = true;
              video.defaultMuted = true;
              video.playsInline = true;
              video.preload = 'auto';
              video.controls = false;

              const playerRoot =
                video.closest('[data-testid="videoComponent"]') ||
                video.closest('[data-testid="videoPlayer"]') ||
                video.parentElement ||
                root;

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
                  await playFor(650);
                } finally {
                  pauseVideo();
                  await wait(140);
                }
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
                    once(video, 'seeked', 3500),
                    once(video, 'timeupdate', 3500),
                  ]);
                  video.currentTime = nextTime;
                  await seekPromise;
                  await waitUntil(
                    () => Math.abs((video.currentTime || 0) - nextTime) <= 0.18,
                    1500,
                  );
                } catch (error) {
                }
                return nextTime;
              };

              const hideVideoOverlays = () => {
                const videoRect = video.getBoundingClientRect();
                const videoArea = videoRect.width * videoRect.height;
                if (videoArea < 1) {
                  return;
                }

                const overlapRatio = (rect) => {
                  const width = Math.max(0, Math.min(videoRect.right, rect.right) - Math.max(videoRect.left, rect.left));
                  const height = Math.max(0, Math.min(videoRect.bottom, rect.bottom) - Math.max(videoRect.top, rect.top));
                  return (width * height) / videoArea;
                };

                const overlayNodes = playerRoot
                  ? playerRoot.querySelectorAll('img, button, [role="button"], svg')
                  : [];

                overlayNodes.forEach((node) => {
                  if (!(node instanceof HTMLElement) || node === video || node.contains(video)) {
                    return;
                  }
                  const rect = node.getBoundingClientRect();
                  if (rect.width < 20 || rect.height < 20) {
                    return;
                  }
                  if (overlapRatio(rect) < 0.55) {
                    return;
                  }
                  node.style.opacity = '0';
                  node.style.pointerEvents = 'none';
                });
              };

              const renderTargetFrame = async (desiredTime, duration) => {
                try {
                  activatePlayer();
                  const playPromise = video.play();
                  if (playPromise && typeof playPromise.then === 'function') {
                    await Promise.race([playPromise.catch(() => undefined), wait(250)]);
                  } else {
                    await wait(180);
                  }

                  await waitUntil(
                    () => {
                      const current = video.currentTime || 0;
                      return current >= Math.max(desiredTime - 0.12, 0);
                    },
                    1800,
                  );
                } catch (error) {
                } finally {
                  pauseVideo();
                  await wait(160);
                }

                if (Math.abs((video.currentTime || 0) - desiredTime) > 0.35) {
                  await seekTo(desiredTime, duration);
                  pauseVideo();
                  await wait(120);
                }
              };

              await ensureMetadata();
              if (video.readyState < 2) {
                await warmUp();
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
              if (video.readyState < 2) {
                await warmUp();
                await seekTo(desiredTime, duration);
              }

              await renderTargetFrame(desiredTime, duration);
              if (Math.abs((video.currentTime || 0) - desiredTime) > 0.35) {
                await seekTo(desiredTime, duration);
                await renderTargetFrame(desiredTime, duration);
              }

              hideVideoOverlays();
              await wait(160);
              return Number.isFinite(video.currentTime) ? video.currentTime : desiredTime;
            }
            """,
            target_seconds,
        )
    except Exception:
        return None

    if isinstance(result, (int, float)):
        return max(0.0, float(result))
    return None


def _compute_capture_clip(page, tweet_card):
    element = tweet_card.element_handle(timeout=5000)
    if element is None:
        return None

    return page.evaluate(
        """
        (el) => {
          const doc = document.documentElement;
          const rootRect = el.getBoundingClientRect();
          const mediaSelector = 'img, svg, video, canvas, picture, iframe';
          const excludedSelector = [
            '[data-testid="logged_out_read_replies_pivot"]',
            '[data-testid="inline_reply_offscreen"]',
            '[data-testid="tweetTextarea_0"]',
            '[data-testid="inline_reply_composer"]',
            '[contenteditable="true"][role="textbox"]',
            '[role="textbox"]',
            'form[aria-label*="Reply"]',
            'form[aria-label*="reply"]',
            'form[aria-label*="回复"]',
            'form[aria-label*="回覆"]'
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

          for (const node of el.querySelectorAll(mediaSelector)) {
            if (!isVisible(node)) {
              continue;
            }
            if (shouldExclude(node)) {
              continue;
            }
            for (const rect of node.getClientRects()) {
              addRect(rect);
            }
          }

          const actionGroups = [...el.querySelectorAll('[role="group"]')]
            .filter((node) => isVisible(node) && !shouldExclude(node))
            .map((node) => {
              const rect = node.getBoundingClientRect();
              return {
                top: rect.top + window.scrollY,
                bottom: rect.bottom + window.scrollY,
                width: rect.width,
                height: rect.height,
              };
            })
            .filter((rect) => rect.width > 40 && rect.height > 12);

          const actionBar = actionGroups.sort((a, b) => b.bottom - a.bottom)[0];

          if (!Number.isFinite(left)) {
            const rootX = rootRect.left + window.scrollX;
            const rootY = rootRect.top + window.scrollY;
            left = rootX;
            top = rootY;
            right = rootX + rootRect.width;
            bottom = rootY + rootRect.height;
          }

          const padding = 12;
          const x = Math.max(0, Math.floor(left - padding));
          const y = Math.max(0, Math.floor(top - padding));
          const maxRight = Math.max(doc.scrollWidth, right + padding);
          const contentBottom = actionBar ? Math.min(bottom, actionBar.bottom) : bottom;
          const maxBottom = Math.max(doc.scrollHeight, contentBottom + padding);
          const width = Math.max(1, Math.ceil(Math.min(maxRight, right + padding) - x));
          const height = Math.max(1, Math.ceil(Math.min(maxBottom, contentBottom + padding) - y));

          return { x, y, width, height };
        }
        """,
        arg=element,
    )


def _capture_detail_snapshot(page, tweet_card, path: Path) -> None:
    clip = _compute_capture_clip(page, tweet_card)
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


def capture_tweet_page(
    url: str,
    output_dir: Path | str,
    profile_dir: Path | str,
    *,
    headless: bool = True,
    dark_mode: bool = True,
    video_timestamp_seconds: float | None = None,
    translate_body: bool = True,
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
    wait_timeout_ms = 90000 if not headless else 25000

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(browser_profile),
            headless=headless,
            viewport={"width": 1280, "height": 1800},
            device_scale_factor=2,
            locale="zh-CN",
            color_scheme="dark" if dark_mode else "light",
            ignore_https_errors=True,
            args=["--disable-blink-features=AutomationControlled"],
        )

        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(wait_timeout_ms)
            page.set_default_navigation_timeout(wait_timeout_ms)
            page.emulate_media(color_scheme="dark" if dark_mode else "light")

            last_error: Exception | None = None

            for candidate_url, mode in _candidate_urls(normalized_url, screen_name, tweet_id):
                try:
                    active_url = candidate_url
                    if mode == "embed_card" and dark_mode:
                        active_url = f"{candidate_url}&theme=dark"

                    page.goto(active_url, wait_until="domcontentloaded")
                    try:
                        page.wait_for_load_state("networkidle", timeout=5000)
                    except PlaywrightTimeoutError:
                        pass

                    _dismiss_common_overlays(page)

                    tweet_card = _wait_for_tweet_card(
                        page,
                        tweet_id,
                        mode,
                        wait_timeout_ms if mode == "detail_page" else 12000,
                    )
                    if tweet_card is None:
                        raise RuntimeError("页面里没有找到可截图的推文主体")

                    page.add_style_tag(
                        content=_detail_capture_css(dark_mode) if mode == "detail_page" else _embed_capture_css(dark_mode)
                    )
                    _dismiss_common_overlays(page)
                    _wait_for_tweet_assets(page, tweet_card)
                    if translate_body:
                        _inject_chinese_translations(tweet_card)
                        _remove_native_translation_ui(tweet_card)
                    _scroll_tweet_into_view(page, tweet_card)
                    page.wait_for_timeout(250)
                    video_frame_seconds = _prepare_video_frame(tweet_card, video_timestamp_seconds)

                    used_url = active_url
                    capture_mode = mode

                    if mode == "detail_page":
                        _capture_detail_snapshot(page, tweet_card, saved_to)
                    else:
                        tweet_card.screenshot(
                            path=str(saved_to),
                            animations="disabled",
                        )
                    break
                except Exception as exc:
                    last_error = exc
                    continue
            else:
                detail = (
                    "可能是链接无效、推文已删除，或该推文需要先登录 X 才能查看。"
                    " 如果需要登录，请勾选页面里的“显示浏览器”后重新截图。"
                )
                if last_error:
                    raise RuntimeError(detail) from last_error
                raise RuntimeError(detail)
        finally:
            context.close()

    return CaptureResult(
        file_name=file_name,
        file_path=saved_to,
        preview_url=f"/screenshots/{file_name}",
        capture_mode=capture_mode,
        used_url=used_url,
        tweet_id=tweet_id,
        video_frame_seconds=video_frame_seconds,
    )
