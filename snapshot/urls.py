"""Resource snapshot: urls."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse
import re
from .config import (
    TWEET_URL_RE,
)


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
