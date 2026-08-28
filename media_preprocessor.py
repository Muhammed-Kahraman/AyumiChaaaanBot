"""Local FFmpeg/FFprobe preprocessing for spoiler analysis."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field

from config import (
    AI_AUDIO_BITRATE_KBPS,
    AI_FRAME_JPEG_QUALITY,
    AI_FRAME_MAX_DIMENSION,
    AI_MAX_FRAMES,
    AI_MAX_VIDEO_DURATION_SECONDS,
    AI_TIMEOUT_SECONDS,
    FFMPEG_PATH,
    FFPROBE_PATH,
)

logger = logging.getLogger(__name__)


class MediaPreprocessingError(RuntimeError):
    pass


@dataclass
class VideoMetadata:
    duration: float
    width: int = 0
    height: int = 0
    has_audio: bool = False


@dataclass
class PreparedVideo:
    metadata: VideoMetadata
    frames: list[str] = field(default_factory=list)
    audio_parts: list[tuple[float, str]] = field(default_factory=list)
    is_partial: bool = False


async def _run(command: list[str], timeout: int = AI_TIMEOUT_SECONDS) -> bytes:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise MediaPreprocessingError(str(exc)) from exc

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise MediaPreprocessingError(f"Komut zaman aşımı: {command[0]}") from exc

    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace")[-600:]
        raise MediaPreprocessingError(detail or f"Komut başarısız: {command[0]}")
    return stdout


async def probe_video(path: str) -> VideoMetadata:
    raw = await _run(
        [
            FFPROBE_PATH,
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height",
            "-of",
            "json",
            path,
        ]
    )
    try:
        data = json.loads(raw)
        duration = float(data.get("format", {}).get("duration", 0))
        streams = data.get("streams", [])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MediaPreprocessingError("ffprobe sonucu okunamadı") from exc

    video_stream = next(
        (stream for stream in streams if stream.get("codec_type") == "video"), None
    )
    if not video_stream or duration <= 0:
        raise MediaPreprocessingError("Geçerli video akışı bulunamadı")

    return VideoMetadata(
        duration=duration,
        width=int(video_stream.get("width") or 0),
        height=int(video_stream.get("height") or 0),
        has_audio=any(stream.get("codec_type") == "audio" for stream in streams),
    )


def frame_count_for_duration(duration: float, maximum: int = AI_MAX_FRAMES) -> int:
    if duration <= 30:
        desired = 4
    elif duration <= 120:
        desired = 6
    elif duration <= 300:
        desired = 8
    elif duration <= 600:
        desired = 10
    else:
        desired = 12
    return max(1, min(maximum, desired))


def representative_timestamps(duration: float, count: int) -> list[float]:
    """Uniform full-timeline samples, avoiding likely black first/last frames."""
    if count <= 1:
        return [max(0.0, duration / 2)]
    margin = min(1.0, duration * 0.03)
    start = margin
    end = max(start, duration - margin)
    step = (end - start) / (count - 1)
    return [round(start + index * step, 3) for index in range(count)]


def analysis_audio_windows(duration: float, maximum_seconds: int) -> list[tuple[float, float]]:
    """Use full audio when possible; otherwise sample start/middle/end windows."""
    if duration <= maximum_seconds:
        return [(0.0, duration)]

    part = maximum_seconds / 3
    return [
        (0.0, part),
        (max(0.0, duration / 2 - part / 2), part),
        (max(0.0, duration - part), part),
    ]


async def _extract_frame(video_path: str, timestamp: float, output_path: str) -> None:
    await _run(
        [
            FFMPEG_PATH,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            str(timestamp),
            "-i",
            video_path,
            "-frames:v",
            "1",
            "-vf",
            (
                f"scale={AI_FRAME_MAX_DIMENSION}:{AI_FRAME_MAX_DIMENSION}:"
                "force_original_aspect_ratio=decrease"
            ),
            "-q:v",
            str(AI_FRAME_JPEG_QUALITY),
            "-y",
            output_path,
        ]
    )


async def _extract_audio(
    video_path: str,
    start: float,
    duration: float,
    output_path: str,
) -> None:
    await _run(
        [
            FFMPEG_PATH,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            str(start),
            "-t",
            str(duration),
            "-i",
            video_path,
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-b:a",
            f"{AI_AUDIO_BITRATE_KBPS}k",
            "-y",
            output_path,
        ]
    )


async def prepare_video(video_path: str, work_dir: str) -> PreparedVideo:
    metadata = await probe_video(video_path)
    analysis_dir = os.path.join(work_dir, "analysis")
    os.makedirs(analysis_dir, exist_ok=True)

    timestamps = representative_timestamps(
        metadata.duration, frame_count_for_duration(metadata.duration)
    )
    frames: list[str] = []
    for index, timestamp in enumerate(timestamps):
        output = os.path.join(analysis_dir, f"frame_{index:02d}_{timestamp:.3f}.jpg")
        try:
            await _extract_frame(video_path, timestamp, output)
            frames.append(output)
        except MediaPreprocessingError as exc:
            logger.warning("Video karesi çıkarılamadı (%.1fs): %s", timestamp, exc)

    if not frames:
        raise MediaPreprocessingError("Videodan hiç temsilî kare çıkarılamadı")

    audio_parts: list[tuple[float, str]] = []
    is_partial = metadata.duration > AI_MAX_VIDEO_DURATION_SECONDS
    if metadata.has_audio:
        windows = analysis_audio_windows(
            metadata.duration, AI_MAX_VIDEO_DURATION_SECONDS
        )
        for index, (start, duration) in enumerate(windows):
            output = os.path.join(analysis_dir, f"audio_{index:02d}.mp3")
            try:
                await _extract_audio(video_path, start, duration, output)
                if os.path.getsize(output) > 0:
                    audio_parts.append((start, output))
            except (MediaPreprocessingError, OSError) as exc:
                is_partial = True
                logger.warning("Video sesi çıkarılamadı (%.1fs): %s", start, exc)

    return PreparedVideo(
        metadata=metadata,
        frames=frames,
        audio_parts=audio_parts,
        is_partial=is_partial,
    )


async def prepare_image(image_path: str, work_dir: str, index: int) -> str:
    analysis_dir = os.path.join(work_dir, "analysis")
    os.makedirs(analysis_dir, exist_ok=True)
    output = os.path.join(analysis_dir, f"image_{index:02d}.jpg")
    await _run(
        [
            FFMPEG_PATH,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            image_path,
            "-vf",
            (
                f"scale={AI_FRAME_MAX_DIMENSION}:{AI_FRAME_MAX_DIMENSION}:"
                "force_original_aspect_ratio=decrease"
            ),
            "-q:v",
            str(AI_FRAME_JPEG_QUALITY),
            "-frames:v",
            "1",
            "-y",
            output,
        ]
    )
    return output
