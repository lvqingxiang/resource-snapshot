"""Resource snapshot: config."""

from __future__ import annotations

import re

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

DEFAULT_LOCALE = "zh-CN"

DEFAULT_TIMEZONE = "Asia/Shanghai"

HLS_MASTER_ROUTE_RE = re.compile(
    r"https://video\.twimg\.com/.*\.m3u8(?:\?.*)?$",
    re.IGNORECASE,
)

# Soft ceiling for headless Chromium: strip 4K / absurdly large amplify
# streams (e.g. 2160x3840) that often fail with MEDIA_ELEMENT_ERROR.
# Pin the best under-cap variant so X's ABR player cannot settle on a soft
# lower rung. Decode failures reload with progressively lower ceilings.
HLS_MAX_EDGE_PX = 2560

HLS_MAX_AREA_PX = 2560 * 1440

# Progressive ceilings used when the current playlist still won't decode.
HLS_DEMOTE_STEPS: tuple[tuple[int, int], ...] = (
    (1920, 1920 * 1080),
    (1280, 1280 * 720),
    (854, 854 * 480),
    (640, 640 * 360),
)

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
