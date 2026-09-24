"""Resource snapshot: styles."""

from __future__ import annotations

from .config import (
    TRANSLATION_ATTR,
)


def _translation_capture_css(dark_mode: bool) -> str:
    background = "rgba(29, 155, 240, 0.12)" if dark_mode else "rgba(29, 155, 240, 0.10)"
    text = "#e7e9ea" if dark_mode else "#0f1419"
    muted = "#8ecdfd" if dark_mode else "#1d6fa5"
    border = "#1d9bf0"

    return f"""
[{TRANSLATION_ATTR}="block"] {{
  display: block !important;
  margin-top: 10px !important;
  padding: 10px 12px !important;
  border-left: 3px solid {border} !important;
  border-radius: 14px !important;
  background: {background} !important;
  color: {text} !important;
  font-family: inherit !important;
  white-space: pre-wrap !important;
  word-break: break-word !important;
}}
[{TRANSLATION_ATTR}="label"] {{
  display: block !important;
  margin: 0 0 6px 0 !important;
  color: {muted} !important;
  font-family: inherit !important;
  font-size: 15px !important;
  font-weight: 600 !important;
  line-height: 1.35 !important;
}}
[{TRANSLATION_ATTR}="body"] {{
  display: block !important;
  margin: 0 !important;
  color: {text} !important;
  font-family: inherit !important;
  font-size: 16px !important;
  font-weight: 400 !important;
  line-height: 1.5 !important;
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
  object-fit: contain !important;
  object-position: center center !important;
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
