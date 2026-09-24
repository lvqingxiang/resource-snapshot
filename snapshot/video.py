"""Resource snapshot: video."""

from __future__ import annotations

import re
from .models import (
    VideoFrameInfo,
)


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
              const publicFallbackVideo = Boolean(video.closest('[data-public-api-media-grid]'));

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
                  await playFor(publicFallbackVideo ? 350 : 1200);
                } finally {
                  pauseVideo();
                  await wait(publicFallbackVideo ? 80 : 180);
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
              // Use the rendered media size as the target so ABR does not settle
              // for a tiny first rung that looks soft once captured.
              const renderedRect = video.getBoundingClientRect();
              const renderedShortEdge = Math.min(renderedRect.width || 0, renderedRect.height || 0);
              const minDecodeWidth = Math.min(
                960,
                Math.max(360, Math.round(renderedShortEdge || 640)),
              );
              await warmUp();
              await waitForDecodedFrame(minDecodeWidth, publicFallbackVideo ? 2500 : 8000);
              if (video.readyState < 2 || video.videoWidth < minDecodeWidth) {
                await warmUp();
                await waitForDecodedFrame(minDecodeWidth, publicFallbackVideo ? 2500 : 8000);
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

              await waitForDecodedFrame(minDecodeWidth, publicFallbackVideo ? 2000 : 6000);
              await wait(publicFallbackVideo ? 120 : 280);

              hideVideoOverlays();
              await wait(publicFallbackVideo ? 60 : 160);
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
