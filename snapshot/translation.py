"""Resource snapshot: translation."""

from __future__ import annotations

from html import unescape
from urllib.parse import urlencode
from urllib.request import Request
from urllib.request import urlopen
import json
import re
import time

from .cache import ttl_cache
from .config import (
    GOOGLE_TRANSLATE_API_URL,
    MYMEMORY_TRANSLATE_API_URL,
    TRANSLATION_ATTR,
    TWITTER_OEMBED_API_URL,
    _CHINESE_CHAR_RE,
    _LATIN_LETTER_RE,
    _LOOKS_MOSTLY_CHINESE_JS,
    _NON_CHINESE_SCRIPT_RE,
    _TEXT_ANCHOR_COLLECTION_JS,
)
from .urls import (
    _extract_status_id_from_url,
)


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


@ttl_cache(ttl=600, cacheable=lambda result: bool(result[0]))
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


@ttl_cache(ttl=600, cacheable=bool)
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
