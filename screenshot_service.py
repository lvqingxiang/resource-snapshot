#!/usr/bin/env python3
"""Capture a screenshot for a single X/Twitter status URL."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
from urllib.parse import urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


TWEET_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:x\.com|twitter\.com|mobile\.twitter\.com)/"
    r"(?P<screen_name>[^/?#]+)/status/(?P<tweet_id>\d+)",
    re.IGNORECASE,
)

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
"""


def _embed_capture_css(dark_mode: bool) -> str:
    background = "#15202b" if dark_mode else "#ffffff"
    return f"""
html, body {{
  margin: 0 !important;
  background: {background} !important;
  color-scheme: {"dark" if dark_mode else "light"} !important;
}}
"""


@dataclass(frozen=True)
class CaptureResult:
    file_name: str
    file_path: Path
    preview_url: str
    capture_mode: str
    used_url: str
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

    page.wait_for_timeout(1200)


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
            for (const rect of node.getClientRects()) {
              addRect(rect);
            }
          }

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
          const maxBottom = Math.max(doc.scrollHeight, bottom + padding);
          const width = Math.max(1, Math.ceil(Math.min(maxRight, right + padding) - x));
          const height = Math.max(1, Math.ceil(Math.min(maxBottom, bottom + padding) - y));

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


def _build_output_name(screen_name: str, tweet_id: str) -> str:
    safe_name = re.sub(r"[^a-zA-Z0-9_]+", "_", screen_name).strip("_") or "tweet"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{safe_name}_{tweet_id}_{timestamp}.png"


def capture_tweet_page(
    url: str,
    output_dir: Path | str,
    profile_dir: Path | str,
    *,
    headless: bool = True,
    dark_mode: bool = True,
) -> CaptureResult:
    normalized_url = _normalize_input_url(url)
    screen_name, tweet_id = _extract_parts(normalized_url)

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    browser_profile = Path(profile_dir)
    browser_profile.mkdir(parents=True, exist_ok=True)

    file_name = _build_output_name(screen_name, tweet_id)
    saved_to = output_path / file_name
    used_url = ""
    capture_mode = ""
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
                    _scroll_tweet_into_view(page, tweet_card)
                    _wait_for_tweet_assets(page, tweet_card)

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
    )
