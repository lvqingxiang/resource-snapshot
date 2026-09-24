"""Resource snapshot: render."""

from __future__ import annotations

from .dom import (
    _expand_tweet_text,
    _hide_non_primary_columns,
)
from .public_data import (
    _collect_node_media_items,
    _fetch_public_x_status,
)
from .styles import (
    _detail_capture_css,
    _translation_capture_css,
)


def _public_media_cell_html(item: dict[str, object]) -> str:
    if item.get("type") == "video":
        return (
            '<div data-testid="videoPlayer" class="media-cell video-cell">'
            f'<video src="{_html_escape(item.get("url"))}" '
            f'poster="{_html_escape(item.get("poster"))}" '
            'muted playsinline preload="auto"></video>'
            "</div>"
        )
    return (
        f'<div data-testid="tweetPhoto" class="media-cell">'
        f'<img src="{_html_escape(item.get("url"))}" loading="eager"></div>'
    )


def _public_media_grid_html(items: list[dict[str, object]], extra_class: str = "") -> str:
    chosen = items[:4]
    if not chosen:
        return ""
    cls = f"media-grid n{len(chosen)}"
    if extra_class:
        cls += f" {extra_class}"
    cells = "".join(_public_media_cell_html(item) for item in chosen)
    return f'<div class="{cls}" data-public-api-media-grid="true">{cells}</div>'


def _html_escape(value: object) -> str:
    import html as _html
    return _html.escape(str(value or ""), quote=True)


def _status_text(status: dict) -> str:
    text = status.get("text")
    if isinstance(text, str):
        return text
    raw = status.get("raw_text")
    if isinstance(raw, dict) and isinstance(raw.get("text"), str):
        return raw["text"]
    return ""


def _render_public_status_card(page, status: dict, tweet_id: str, *, dark_mode: bool, source: str):
    """Render public API data into an X-detail-like DOM so the original capture pipeline can reuse it.

    Public fallback media deliberately does NOT use data-resource-snapshot-media-grid,
    because _prepare_tweet_media_for_screenshot() clears that attribute as stale
    generated media before rebuilding real X carousels. Using it here would delete
    the fallback images before capture.
    """
    import datetime as _dt
    from email.utils import parsedate_to_datetime as _parsedate_to_datetime

    author = status.get("author") if isinstance(status.get("author"), dict) else {}
    name = author.get("name") or author.get("screen_name") or "X user"
    handle = author.get("screen_name") or ""
    avatar = author.get("avatar_url") or author.get("avatar") or ""
    text = _status_text(status)
    media_items = _collect_node_media_items(status)
    quote = status.get("quote") if isinstance(status.get("quote"), dict) else None

    bg = "#000000" if dark_mode else "#ffffff"
    fg = "#e7e9ea" if dark_mode else "#0f1419"
    muted = "#71767b" if dark_mode else "#536471"
    border = "#2f3336" if dark_mode else "#eff3f4"

    def metric(*names: str) -> object:
        for key in names:
            value = status.get(key)
            if value not in (None, ""):
                return value
        return ""

    def _number(value: object) -> float | None:
        if value in (None, ""):
            return None
        try:
            if isinstance(value, str):
                value = value.replace(",", "").strip()
            return float(value)
        except Exception:
            return None

    def fmt_count(value: object) -> str:
        """Use zh-CN count abbreviations like the real X detail page: 万/亿, never K/M."""
        n = _number(value)
        if n is None:
            return ""
        sign = "-" if n < 0 else ""
        n = abs(n)
        if n >= 100_000_000:
            s = f"{n / 100_000_000:.1f}".rstrip("0").rstrip(".")
            return f"{sign}{s}亿"
        if n >= 10_000:
            s = f"{n / 10_000:.1f}".rstrip("0").rstrip(".")
            return f"{sign}{s}万"
        return f"{sign}{int(round(n))}"

    def fmt_time() -> str:
        raw = status.get("created_at") or status.get("created_timestamp") or status.get("date")
        if raw in (None, ""):
            return ""
        try:
            if isinstance(raw, (int, float)) or (isinstance(raw, str) and raw.strip().isdigit()):
                ts = int(float(raw))
                if ts > 10_000_000_000:
                    ts //= 1000
                dt = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc)
            else:
                raw_s = str(raw).strip()
                try:
                    dt = _dt.datetime.fromisoformat(raw_s.replace("Z", "+00:00"))
                except Exception:
                    dt = _parsedate_to_datetime(raw_s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_dt.timezone.utc)
            dt = dt.astimezone(_dt.timezone(_dt.timedelta(hours=8)))
            return f"{dt.hour:02d}:{dt.minute:02d} · {dt.year}年{dt.month}月{dt.day}日"
        except Exception:
            return ""

    image_html = _public_media_grid_html(media_items)

    quote_html = ""
    if quote:
        qa = quote.get("author") if isinstance(quote.get("author"), dict) else {}
        qname = qa.get("name") or qa.get("screen_name") or ""
        qhandle = qa.get("screen_name") or ""
        qtext = _status_text(quote)
        qmedia = _public_media_grid_html(_collect_node_media_items(quote), extra_class="quote-media")
        quote_html = (
            '<div data-testid="quoteTweet" class="quote">'
            f'<div data-testid="User-Name" class="quote-user"><b>{_html_escape(qname)}</b> <span>@{_html_escape(qhandle)}</span></div>'
            f'<div data-testid="tweetText" class="quote-text">{_html_escape(qtext)}</div>{qmedia}'
            '</div>'
        )
        if quote.get("type") == "tombstone":
            quote_html = (
                '<div data-testid="quoteTweet" class="quote">'
                '<div class="quote-text">引用帖暂时无法加载</div></div>'
            )

    reply = fmt_count(metric("replies", "reply_count"))
    repost = fmt_count(metric("retweets", "reposts", "retweet_count"))
    likes = fmt_count(metric("likes", "favorites", "like_count"))
    bookmarks = fmt_count(metric("bookmarks", "bookmark_count"))
    views = fmt_count(metric("views", "view_count", "viewCount"))
    created = fmt_time()

    avatar_html = (
        f'<img class="avatar" src="{_html_escape(avatar)}" loading="eager">'
        if avatar else '<div class="avatar"></div>'
    )

    icons = {
        "reply": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M1.751 10c0-4.42 3.584-8 8.005-8h4.366c4.49 0 8.129 3.64 8.129 8.13 0 2.96-1.607 5.68-4.196 7.11l-8.054 4.46v-3.69h-.067c-4.49.1-8.183-3.51-8.183-8.01zm8.005-6c-3.317 0-6.005 2.69-6.005 6 0 3.37 2.77 6.08 6.138 6.01l.351-.01h1.761v2.3l5.087-2.81c1.951-1.08 3.163-3.13 3.163-5.36 0-3.39-2.744-6.13-6.129-6.13H9.756z"/></svg>',
        "retweet": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4.5 3.88l4.432 4.14-1.364 1.46L5.5 7.55V16c0 1.1.896 2 2 2H13v2H7.5c-2.209 0-4-1.79-4-4V7.55L1.432 9.48.068 8.02 4.5 3.88zM16.5 6H11V4h5.5c2.209 0 4 1.79 4 4v8.45l2.068-1.93 1.364 1.46-4.432 4.14-4.432-4.14 1.364-1.46 2.068 1.93V8c0-1.1-.896-2-2-2z"/></svg>',
        "like": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M16.697 5.5c-1.222-.06-2.679.51-3.89 2.16l-.805 1.09-.806-1.09C9.984 6.01 8.526 5.44 7.304 5.5c-1.243.07-2.349.78-2.91 1.91-.552 1.12-.633 2.78.479 4.82 1.074 1.97 3.257 4.27 7.129 6.61 3.87-2.34 6.052-4.64 7.126-6.61 1.111-2.04 1.03-3.7.477-4.82-.561-1.13-1.666-1.84-2.908-1.91zm4.187 7.69c-1.351 2.48-4.001 5.12-8.379 7.67l-.503.3-.504-.3c-4.379-2.55-7.029-5.19-8.382-7.67-1.36-2.5-1.41-4.86-.514-6.67.887-1.79 2.647-2.91 4.601-3.01 1.651-.09 3.368.56 4.798 2.01 1.429-1.45 3.146-2.1 4.796-2.01 1.954.1 3.714 1.22 4.601 3.01.896 1.81.846 4.17-.514 6.67z"/></svg>',
        "bookmark": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 4.5C4 3.12 5.119 2 6.5 2h11C18.881 2 20 3.12 20 4.5v18.44l-8-5.71-8 5.71V4.5zM6.5 4c-.276 0-.5.22-.5.5v14.56l6-4.29 6 4.29V4.5c0-.28-.224-.5-.5-.5h-11z"/></svg>',
        "share": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2.59l5.7 5.7-1.41 1.42L13 6.41V16h-2V6.41l-3.3 3.3-1.41-1.42L12 2.59zM21 15l-.02 3.51c0 1.38-1.12 2.49-2.5 2.49H5.5C4.11 21 3 19.88 3 18.5V15h2v3.5c0 .28.22.5.5.5h12.98c.28 0 .5-.22.5-.5L19 15h2z"/></svg>',
    }

    def action(testid: str, icon_name: str, value: str, aria: str) -> str:
        normalized_value = str(value).strip()
        value_html = (
            f'<span class="count">{_html_escape(value)}</span>'
            if normalized_value not in {"", "0", "0.0"}
            else ""
        )
        return (
            f'<button type="button" data-testid="{testid}" aria-label="{_html_escape(aria)}" class="action">'
            f'<span class="icon">{icons[icon_name]}</span>{value_html}</button>'
        )

    meta_parts = []
    if created:
        meta_parts.append(f'<time>{_html_escape(created)}</time>')
    if views:
        meta_parts.append(f'<span class="views"><b>{_html_escape(views)}</b> <span>Views</span></span>')
    meta_html = '<span class="dot"> · </span>'.join(meta_parts)

    html = f'''<!doctype html><html><head><meta charset="utf-8"><style>
      *{{box-sizing:border-box}}
      html,body{{margin:0;background:{bg};color:{fg};font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;}}
      body{{padding:10px 16px;}}
      main[role="main"]{{display:block;background:{bg};}}
      article[data-testid="tweet"]{{width:598px;max-width:598px;margin:0 auto;background:{bg};color:{fg};overflow:visible;padding:0 0 10px;}}
      .user{{display:flex;gap:10px;align-items:center;padding:8px 0 0;}}
      .avatar{{width:40px;height:40px;border-radius:50%;object-fit:cover;background:{border};flex:none;}}
      .identity{{min-width:0;line-height:1.18;}}
      .name{{font-weight:700;font-size:15px;color:{fg};}}
      .handle{{color:{muted};font-size:15px;margin-top:2px;}}
      [data-testid="tweetText"]{{font-size:20px;line-height:1.42;white-space:pre-wrap;overflow-wrap:anywhere;margin:14px 0 10px;color:{fg};}}
      .media-grid{{display:grid;gap:2px;border-radius:16px;overflow:hidden;border:1px solid {border};margin-top:10px;background:{border};width:100%;}}
      .media-grid.n1{{grid-template-columns:1fr}} .media-grid.n2{{grid-template-columns:1fr 1fr;aspect-ratio:16 / 9}}
      .media-grid.n3{{grid-template-columns:1fr 1fr;grid-template-rows:1fr 1fr;aspect-ratio:4 / 3}} .media-grid.n4{{grid-template-columns:1fr 1fr;grid-template-rows:1fr 1fr;aspect-ratio:7 / 8}}
      .media-grid.n3 .media-cell:nth-child(1){{grid-row:1 / span 2}}
      .media-cell{{min-height:220px;max-height:720px;background:{border};overflow:hidden;}}
      .media-grid.n2 .media-cell,.media-grid.n3 .media-cell,.media-grid.n4 .media-cell{{min-height:0;max-height:none;height:100%;}}
      .media-cell img{{width:100%;height:100%;max-height:720px;object-fit:cover;display:block;}}
      .media-grid.n1 .media-cell img{{height:auto;object-fit:contain;}}
      .video-cell{{display:flex;align-items:center;justify-content:center;}}
      .media-grid.n1 .video-cell{{aspect-ratio:auto;min-height:0;max-height:none;}}
      .video-cell video{{width:100%;height:100%;object-fit:contain;object-position:center;display:block;background:#000;}}
      .media-grid.n1 .video-cell video{{height:auto;}}
      .quote{{border:1px solid {border};border-radius:14px;padding:12px;margin-top:12px;overflow:hidden;}}
      .quote-user{{font-size:15px;line-height:1.3}} .quote-user span{{color:{muted}}}
      .quote-text{{font-size:15px!important;line-height:1.45!important;margin:8px 0!important;}}
      .quote-media{{margin-top:8px;}}
      .meta{{display:flex;align-items:center;flex-wrap:wrap;color:{muted};font-size:15px;border-bottom:1px solid {border};padding:14px 0 12px;margin-top:2px;line-height:1.3;}}
      .meta b{{color:{fg};font-weight:700;}} .meta .dot{{color:{muted};white-space:pre;}}
      .actions{{display:grid;grid-template-columns:repeat(5,1fr);align-items:center;color:{muted};padding:8px 0 0;margin:0;width:100%;}}
      .action{{appearance:none;border:0;background:transparent;color:{muted};padding:4px 8px;display:flex;align-items:center;justify-content:flex-start;gap:7px;min-width:0;font:inherit;}}
      .action:nth-child(5){{justify-content:flex-end;}}
      .icon{{width:20px;height:20px;display:inline-flex;align-items:center;justify-content:center;flex:none;}}
      .icon svg{{width:20px;height:20px;fill:currentColor;display:block;}}
      .count{{font-size:14px;line-height:20px;color:{muted};white-space:nowrap;}}
      article[data-resource-snapshot-compact] .actions{{grid-template-columns:repeat(5,max-content);justify-content:space-between;column-gap:10px;padding:12px 4px 0!important;}}
      article[data-resource-snapshot-compact] .action{{padding:4px 0;gap:4px;}}
      article[data-resource-snapshot-compact] .icon,
      article[data-resource-snapshot-compact] .icon svg{{width:18px;height:18px;}}
      {_translation_capture_css(dark_mode)}
    </style></head><body>
      <main role="main"><article data-testid="tweet" data-tweet-id="{_html_escape(tweet_id)}">
        <a href="https://x.com/{_html_escape(handle)}/status/{_html_escape(tweet_id)}" style="display:none"></a>
        <div class="user">{avatar_html}<div data-testid="User-Name" class="identity"><div class="name">{_html_escape(name)}</div><div class="handle">@{_html_escape(handle)}</div></div></div>
        <div data-testid="tweetText">{_html_escape(text)}</div>{image_html}{quote_html}
        <div class="meta">{meta_html}</div>
        <div role="group" class="actions" aria-label="Engagement">
          {action("reply", "reply", reply, f"Reply {reply}")}
          {action("retweet", "retweet", repost, f"Retweet {repost}")}
          {action("like", "like", likes, f"Like {likes}")}
          {action("bookmark", "bookmark", bookmarks, f"Bookmark {bookmarks}")}
          {action("share", "share", "", "Share")}
        </div>
      </article></main>
    </body></html>'''
    page.set_content(html, wait_until="domcontentloaded")
    page.wait_for_timeout(100)
    card = page.locator(f'article[data-tweet-id="{tweet_id}"]').first
    card.wait_for(state="visible", timeout=5000)
    return card


def _load_public_fallback_tweet_card(
    page,
    tweet_id: str,
    screen_name: str | None = None,
    *,
    dark_mode: bool,
    status: dict | None = None,
    source: str = "",
    timeout: float = 6,
):
    if not isinstance(status, dict):
        status, source = _fetch_public_x_status(tweet_id, screen_name=screen_name, timeout=timeout)
    if not isinstance(status, dict):
        return None, "", ""
    print(f"[X public API] {source} 命中，使用匿名数据兼容截图", flush=True)
    card = _render_public_status_card(page, status, tweet_id, dark_mode=dark_mode, source=source)
    return card, f"public-api://{source}/status/{tweet_id}", "public_api_fallback"


def _prepare_public_fallback_card(page, tweet_id: str, public_card, *, dark_mode: bool):
    _expand_tweet_text(public_card)
    page.wait_for_timeout(100)
    page.add_style_tag(content=_detail_capture_css(dark_mode))
    _hide_non_primary_columns(page, tweet_id)
    return public_card
