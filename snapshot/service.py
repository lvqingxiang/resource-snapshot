"""Resource snapshot: service."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from playwright.sync_api import sync_playwright
from .browser import (
    _demote_hls_quality_gate,
    _open_capture_session,
    _tweet_video_undecodable,
)
from .config import (
    HLS_DEMOTE_STEPS,
)
from .dom import (
    _dismiss_common_overlays,
    _expand_tweet_text,
    _hide_non_primary_columns,
    _scroll_tweet_into_view,
    _wait_for_public_fallback_assets,
    _wait_for_tweet_assets,
    _wait_for_tweet_card,
)
from .layout import (
    _capture_detail_snapshot,
    _prepare_tweet_for_screenshot,
)
from .models import (
    CaptureResult,
    TranslationPreviewItem,
    TranslationPreviewResult,
    VideoFrameInfo,
)
from .public_data import (
    _fetch_public_x_status,
)
from .render import (
    _load_public_fallback_tweet_card,
    _prepare_public_fallback_card,
)
from .styles import (
    _detail_capture_css,
)
from .translation import (
    _build_translation_items,
    _collect_translation_text_blocks,
    _inject_chinese_translations,
    _remove_native_translation_ui,
    _translation_label_for_index,
)
from .urls import (
    _build_output_name,
    _candidate_urls,
    _extract_parts,
    _normalize_input_url,
)
from .video import (
    _prepare_video_frames,
)


def _load_tweet_card(
    page,
    normalized_url: str,
    screen_name: str,
    tweet_id: str,
    *,
    dark_mode: bool,
    wait_timeout_ms: int,
    guest_mode: bool = False,
    prefer_public: bool = False,
):
    # Detail pages that X rejects come back as an empty 403. Prefetch the public
    # card during navigation so that failure does not add another round trip.
    public_pool = None
    public_fetch = None
    if not (guest_mode or prefer_public):
        public_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="x-public-prefetch")
        public_fetch = public_pool.submit(
            _fetch_public_x_status,
            tweet_id,
            screen_name,
            timeout=6,
        )

    try:
        return _load_tweet_card_once(
            page,
            normalized_url,
            screen_name,
            tweet_id,
            dark_mode=dark_mode,
            wait_timeout_ms=wait_timeout_ms,
            guest_mode=guest_mode,
            prefer_public=prefer_public,
            public_fetch=public_fetch,
        )
    finally:
        if public_pool is not None:
            public_pool.shutdown(wait=False, cancel_futures=True)


def _load_tweet_card_once(
    page,
    normalized_url: str,
    screen_name: str,
    tweet_id: str,
    *,
    dark_mode: bool,
    wait_timeout_ms: int,
    guest_mode: bool,
    prefer_public: bool,
    public_fetch,
):
    last_error: Exception | None = None
    public_status: dict | None = None
    public_source = ""

    if guest_mode or prefer_public:
        public_status, public_source = _fetch_public_x_status(tweet_id, screen_name=screen_name, timeout=3)
        if isinstance(public_status, dict):
            try:
                public_card, public_url, public_mode = _load_public_fallback_tweet_card(
                    page,
                    tweet_id,
                    screen_name,
                    dark_mode=dark_mode,
                    status=public_status,
                    source=public_source,
                    timeout=3,
                )
                if public_card is not None:
                    _prepare_public_fallback_card(page, tweet_id, public_card, dark_mode=dark_mode)
                    return public_card, public_url, public_mode
            except Exception as exc:
                last_error = exc

    blocked = False
    for candidate_url, mode in _candidate_urls(normalized_url, screen_name, tweet_id):
        try:
            # X detail pages often stay on readyState=interactive with open media
            # sockets, so waiting for "domcontentloaded" can hang until timeout
            # even after the tweet article is already in the DOM.
            response = page.goto(candidate_url, wait_until="commit")
            status = getattr(response, "status", 0) or 0
            if status in {401, 403, 429}:
                # Empty rejection. Further x.com/twitter.com aliases will not render a card.
                blocked = True
                raise RuntimeError(f"详情页返回 HTTP {status}")
            # Wait for the actual target, not unrelated network activity or a fixed delay.
            if not guest_mode:
                _dismiss_common_overlays(page)

            tweet_card = _wait_for_tweet_card(page, tweet_id, wait_timeout_ms)
            if tweet_card is None:
                raise RuntimeError("页面里没有找到可截图的推文主体")

            _expand_tweet_text(tweet_card)
            page.add_style_tag(content=_detail_capture_css(dark_mode))
            _hide_non_primary_columns(page, tweet_id)
            if not guest_mode:
                _dismiss_common_overlays(page)
            return tweet_card, candidate_url, mode
        except Exception as exc:
            last_error = exc
            if blocked:
                break
            continue

    if public_fetch is not None and not isinstance(public_status, dict):
        try:
            fetched_status, fetched_source = public_fetch.result(timeout=8)
        except Exception as exc:
            last_error = exc
        else:
            if isinstance(fetched_status, dict):
                public_status, public_source = fetched_status, fetched_source

    # Anonymous X detail pages may return an empty/login-gated shell.
    # Fall back to public mirror JSON and render a local tweet-like card instead
    # of forcing the user to log in.
    try:
        public_card, public_url, public_mode = _load_public_fallback_tweet_card(
            page,
            tweet_id,
            screen_name,
            dark_mode=dark_mode,
            status=public_status,
            source=public_source,
            timeout=6,
        )
        if public_card is not None:
            _prepare_public_fallback_card(page, tweet_id, public_card, dark_mode=dark_mode)
            return public_card, public_url, public_mode
    except Exception as exc:
        last_error = exc

    detail = (
        "未能获取该推文的公开内容。X 匿名详情页未返回推文主体，"
        "公开 FxTwitter/VxTwitter 兜底也不可用；推文可能已删除、私密、地区受限，"
        "或当前网络无法访问相关公开服务。"
    )
    if last_error:
        raise RuntimeError(detail) from last_error
    raise RuntimeError(detail)


def preview_tweet_translations(
    url: str,
    profile_dir: Path | str,
    *,
    headless: bool = True,
    anonymous: bool = False,
    dark_mode: bool = True,
) -> TranslationPreviewResult:
    normalized_url = _normalize_input_url(url)
    screen_name, tweet_id = _extract_parts(normalized_url)

    browser_profile = Path(profile_dir)
    browser_profile.mkdir(parents=True, exist_ok=True)
    guest_mode = anonymous
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
        # Translation needs text only. Do not download image/video assets.
        page.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in {"image", "media", "font"}
            else route.continue_(),
        )
        try:
            tweet_card, used_url, capture_mode = _load_tweet_card(
                page,
                normalized_url,
                screen_name,
                tweet_id,
                dark_mode=dark_mode,
                wait_timeout_ms=wait_timeout_ms,
                guest_mode=guest_mode,
                prefer_public=headless,
            )

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
    anonymous: bool = False,
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
    guest_mode = anonymous
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
                _expand_tweet_text(tweet_card)
                _scroll_tweet_into_view(page, tweet_card, guest_mode=guest_mode)
                if capture_mode == "public_api_fallback":
                    _wait_for_public_fallback_assets(page, tweet_card)
                else:
                    _wait_for_tweet_assets(page, tweet_card)
                if translate_body:
                    _inject_chinese_translations(
                        tweet_card,
                        custom_translation=custom_translation,
                        translation_overrides=translation_overrides,
                        status_url=used_url or normalized_url,
                    )
                    _remove_native_translation_ui(tweet_card)
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
            _capture_detail_snapshot(
                page,
                tweet_card,
                saved_to,
                tweet_id=tweet_id,
                public_api_fallback=capture_mode == "public_api_fallback",
                guest_mode=guest_mode,
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
