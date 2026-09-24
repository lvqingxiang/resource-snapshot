"""Resource snapshot: public data."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from concurrent.futures import as_completed
from typing import Callable
from urllib.request import Request
from urllib.request import urlopen
import json
import re
import shutil
import ssl
import subprocess

from .cache import ttl_cache


def _fetch_public_x_json(api_url: str, timeout: float = 6) -> dict | None:
    """Fetch public X mirror JSON without login."""
    curl = shutil.which("curl")
    if curl:
        try:
            completed = subprocess.run(
                [
                    curl,
                    "--silent",
                    "--show-error",
                    "--location",
                    "--insecure",
                    "--connect-timeout",
                    str(timeout),
                    "--max-time",
                    str(timeout),
                    "--header",
                    "User-Agent: resource-snapshot/1.0 anonymous-public-fallback",
                    "--header",
                    "Accept: application/json,text/plain,*/*",
                    api_url,
                ],
                capture_output=True,
                text=True,
                timeout=timeout + 1,
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip()
                print(f"[X public API] {api_url} -> curl exit {completed.returncode}: {detail}", flush=True)
                return None
            payload = json.loads(completed.stdout)
            return payload if isinstance(payload, dict) else None
        except Exception as exc:
            print(f"[X public API] {api_url} -> {type(exc).__name__}: {exc}", flush=True)
            return None

    request = Request(
        api_url,
        headers={
            "User-Agent": "resource-snapshot/1.0 anonymous-public-fallback",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    try:
        context = ssl._create_unverified_context()
        with urlopen(request, timeout=timeout, context=context) as response:
            payload = json.load(response)
        return payload if isinstance(payload, dict) else None
    except Exception as exc:
        print(f"[X public API] {api_url} -> {type(exc).__name__}: {exc}", flush=True)
        return None


def _normalize_vxtwitter_status(data: dict) -> dict | None:
    if not isinstance(data, dict):
        return None
    photos: list[dict] = []
    videos: list[dict] = []
    media_ext = data.get("media_extended")
    if isinstance(media_ext, list):
        for item in media_ext:
            if not isinstance(item, dict):
                continue
            u = item.get("url")
            if not isinstance(u, str) or not u:
                continue
            typ = str(item.get("type") or "").lower()
            if typ in {"video", "gif"} or "video.twimg.com" in u or ".mp4" in u.lower().split("?", 1)[0]:
                videos.append({"type": typ or "video", "url": u, "thumbnail_url": item.get("thumbnail_url")})
            else:
                photos.append({"type": "photo", "url": u})
    if not photos and not videos and isinstance(data.get("mediaURLs"), list):
        for u in data["mediaURLs"]:
            if not isinstance(u, str) or not u:
                continue
            if "video.twimg.com" in u or ".mp4" in u.lower().split("?", 1)[0]:
                videos.append({"type": "video", "url": u})
            elif "twimg.com/media" in u:
                photos.append({"type": "photo", "url": u})
    return {
        "id": data.get("tweetID"),
        "url": data.get("tweetURL"),
        "quote": (
            _normalize_vxtwitter_status(data["qrt"])
            if isinstance(data.get("qrt"), dict)
            else ({"type": "tombstone", "url": data["qrtURL"], "message": "引用帖暂时无法加载"}
                  if data.get("qrtURL") else None)
        ),
        "text": data.get("text") or "",
        "created_timestamp": data.get("date_epoch"),
        "author": {
            "name": data.get("user_name") or data.get("user_screen_name") or "",
            "screen_name": data.get("user_screen_name") or "",
            "avatar_url": data.get("user_profile_image_url") or data.get("user_profile_image_url_https") or "",
        },
        "media": {"photos": photos, "videos": videos, "all": [*photos, *videos]},
        "likes": data.get("likes"),
        "replies": data.get("replies"),
        "retweets": data.get("retweets"),
    }


@ttl_cache(ttl=60, maxsize=64, cacheable=lambda result: result[0] is not None)
def _fetch_public_x_status(
    tweet_id: str,
    screen_name: str | None = None,
    *,
    timeout: float = 6,
) -> tuple[dict | None, str]:
    """Fetch mirror JSON and reconcile quotes before accepting a partial response."""
    candidates: list[tuple[str, str, Callable[[dict], dict | None]]] = []

    def parse_fxtwitter_v2(data: dict) -> dict | None:
        status = data.get("status")
        if isinstance(status, dict) and data.get("code") in (None, 200):
            return status
        return None

    def parse_fxtwitter_v1(data: dict) -> dict | None:
        tweet = data.get("tweet")
        return tweet if isinstance(tweet, dict) else None

    def parse_vxtwitter(data: dict) -> dict | None:
        return _normalize_vxtwitter_status(data)

    if screen_name:
        candidates.append(
            (
                f"https://api.fxtwitter.com/{screen_name}/status/{tweet_id}",
                "fxtwitter_v1_user",
                parse_fxtwitter_v1,
            )
        )
    candidates.extend(
        [
            (f"https://api.fxtwitter.com/2/status/{tweet_id}", "fxtwitter_v2", parse_fxtwitter_v2),
            (f"https://api.fxtwitter.com/status/{tweet_id}", "fxtwitter_v1", parse_fxtwitter_v1),
            (f"https://api.vxtwitter.com/Twitter/status/{tweet_id}", "vxtwitter", parse_vxtwitter),
        ]
    )

    executor = ThreadPoolExecutor(max_workers=len(candidates), thread_name_prefix="x-public-api")
    futures = {
        executor.submit(_fetch_public_x_json, api_url, timeout): (source, parser)
        for api_url, source, parser in candidates
    }
    fallback_status: dict | None = None
    fallback_source = ""
    primary_status: dict | None = None
    primary_source = ""
    best_quote: dict | None = None
    def quote_rank(quote: dict | None) -> int:
        if not quote:
            return 0
        if quote.get("type") == "tombstone":
            return 1
        return 2 if quote.get("author") or quote.get("text") or quote.get("media") else 1

    try:
        for future in as_completed(futures, timeout=timeout + 1):
            source, parser = futures[future]
            try:
                data = future.result()
            except Exception:
                continue
            status = parser(data) if isinstance(data, dict) else None
            if isinstance(status, dict):
                quote = status.get("quote")
                if isinstance(quote, dict) and quote_rank(quote) > quote_rank(best_quote):
                    best_quote = quote
                if source == "vxtwitter":
                    fallback_status = status
                    fallback_source = source
                    continue
                if primary_status is None:
                    primary_status = status
                    primary_source = source
    except FutureTimeoutError:
        pass
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    selected = primary_status if primary_status is not None else fallback_status
    if selected is not None:
        selected = dict(selected)
        if best_quote is not None:
            selected["quote"] = best_quote
        return selected, primary_source or fallback_source
    return None, ""


def _status_has_video_media(status: dict) -> bool:
    nodes = [status]
    quote = status.get("quote") if isinstance(status, dict) else None
    if isinstance(quote, dict):
        nodes.append(quote)

    for node in nodes:
        media = node.get("media") if isinstance(node, dict) else None
        if not isinstance(media, dict):
            continue
        videos = media.get("videos")
        if isinstance(videos, list) and videos:
            return True
        all_media = media.get("all")
        if isinstance(all_media, list):
            for item in all_media:
                if not isinstance(item, dict):
                    continue
                typ = str(item.get("type") or "").lower()
                url = str(item.get("url") or "").lower().split("?", 1)[0]
                if typ in {"video", "gif", "animated_gif"} or "video.twimg.com" in url or url.endswith(".mp4"):
                    return True
    return False


def _media_item_is_video(item: dict) -> bool:
    typ = str(item.get("type") or "").lower()
    url = str(item.get("url") or "").lower().split("?", 1)[0]
    return typ in {"video", "gif", "animated_gif"} or "video.twimg.com" in url or url.endswith(".mp4")


def _choose_status_video_url(item: dict) -> str:
    formats = item.get("formats")
    best_mp4 = ""
    best_score: tuple[int, int] | None = None
    if isinstance(formats, list):
        for fmt in formats:
            if not isinstance(fmt, dict):
                continue
            url = fmt.get("url")
            if not isinstance(url, str) or not url:
                continue
            container = str(fmt.get("container") or "").lower()
            if container and container != "mp4":
                continue
            if ".mp4" not in url.lower().split("?", 1)[0]:
                continue
            match = re.search(r"/(?P<w>\d+)x(?P<h>\d+)/", url)
            short_edge = min(int(match.group("w")), int(match.group("h"))) if match else 0
            bitrate = int(fmt.get("bitrate") or 0)
            target_penalty = abs(min(short_edge, 1080) - 720)
            oversize_penalty = max(short_edge - 1080, 0) * 4
            score = (target_penalty + oversize_penalty, -bitrate)
            if best_score is None or score < best_score:
                best_score = score
                best_mp4 = url

    direct = item.get("url")
    if isinstance(direct, str) and direct and not best_mp4:
        if ".mp4" in direct.lower().split("?", 1)[0]:
            best_mp4 = direct
    return best_mp4


def _collect_node_media_items(node: dict) -> list[dict[str, object]]:
    """Return photos and videos for one status/quote node, preserving API order."""
    media = node.get("media") if isinstance(node, dict) else None
    if not isinstance(media, dict):
        return []

    items: list[dict[str, object]] = []
    seen: set[str] = set()

    def append_photo(url: object) -> None:
        if isinstance(url, str) and url and url not in seen:
            seen.add(url)
            items.append({"type": "photo", "url": url})

    def append_video(item: dict) -> None:
        url = _choose_status_video_url(item)
        if not url or url in seen:
            return
        if not _media_item_is_video({"type": item.get("type"), "url": url}):
            return
        seen.add(url)
        items.append(
            {
                "type": "video",
                "url": url,
                "poster": item.get("thumbnail_url") or item.get("thumbnail") or "",
            }
        )

    all_media = media.get("all")
    if isinstance(all_media, list) and all_media:
        for item in all_media:
            if not isinstance(item, dict):
                continue
            if _media_item_is_video(item):
                append_video(item)
            else:
                typ = str(item.get("type") or "").lower()
                url = item.get("url")
                if typ in {"photo", "image"} or (isinstance(url, str) and "twimg.com/media" in url):
                    append_photo(url)
        if items:
            return items

    photos = media.get("photos")
    if isinstance(photos, list):
        for item in photos:
            if isinstance(item, dict):
                append_photo(item.get("url"))
            elif isinstance(item, str):
                append_photo(item)
    videos = media.get("videos")
    if isinstance(videos, list):
        for item in videos:
            if isinstance(item, dict):
                append_video(item)
    if items:
        return items

    mosaic = media.get("mosaic")
    if isinstance(mosaic, dict):
        fmts = mosaic.get("formats")
        mu = (fmts.get("jpeg") or fmts.get("webp")) if isinstance(fmts, dict) else mosaic.get("url")
        append_photo(mu)
    return items
