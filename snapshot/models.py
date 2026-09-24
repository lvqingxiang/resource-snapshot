"""Resource snapshot: models."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class VideoFrameInfo:
    index: int
    label: str
    seconds: float
    quoted: bool


@dataclass(frozen=True)
class CaptureResult:
    file_name: str
    file_path: Path
    preview_url: str
    capture_mode: str
    used_url: str
    tweet_id: str
    video_frame_seconds: float | None
    video_frames: tuple[VideoFrameInfo, ...] = ()


@dataclass(frozen=True)
class TranslationPreviewItem:
    index: int
    label: str
    original_text: str
    suggested_translation: str


@dataclass(frozen=True)
class TranslationPreviewResult:
    items: tuple[TranslationPreviewItem, ...]
    used_url: str
    capture_mode: str
    tweet_id: str
