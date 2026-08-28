#!/usr/bin/env python3
"""AyumuChanBot — Telegram media downloader with automatic spoiler detection."""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional

from telegram import InputMediaPhoto, Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from config import (
    AI_FAILURE_POLICY,
    AI_JOB_CONCURRENCY,
    AI_SPOILER_ENABLED,
    INSTAGRAM_PATTERN,
    MEDIA_QUEUE_CAPACITY,
    OPENAI_API_KEY,
    TELEGRAM_ALBUM_LIMIT,
    TELEGRAM_BOT_TOKEN,
    X_PATTERN,
)
from downloader import MediaResult, cleanup, download_media
from spoiler_analyzer import OpenAISpoilerAnalyzer, SpoilerDecisionSet

logging.basicConfig(
    format="%(asctime)s [%(name)s] %(levelname)s — %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("AyumuChanBot")
# Bot API URL'si token içerdiği için INFO seviyesinde HTTP isteği loglama.
logging.getLogger("httpx").setLevel(logging.WARNING)


class _SensitiveDataFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for secret in (TELEGRAM_BOT_TOKEN, OPENAI_API_KEY):
            if secret:
                message = message.replace(secret, "[REDACTED]")
        record.msg = message
        record.args = ()
        return True


for _handler in logging.getLogger().handlers:
    _handler.addFilter(_SensitiveDataFilter())


def extract_urls(text: str) -> list[str]:
    """Mesajdan Instagram ve X URL'lerini çıkar."""
    urls: list[str] = []
    for pattern in (INSTAGRAM_PATTERN, X_PATTERN):
        urls.extend(pattern.findall(text))
    return urls


RECENT_URL_TTL = 180
_recent_urls: dict[str, float] = {}


def seen_recently(url: str) -> bool:
    """Link son RECENT_URL_TTL saniye içinde işlendiyse True."""
    key = url.split("?")[0]
    now = time.monotonic()
    for old_key, seen_at in list(_recent_urls.items()):
        if now - seen_at > RECENT_URL_TTL:
            del _recent_urls[old_key]
    if key in _recent_urls:
        return True
    _recent_urls[key] = now
    return False


def build_media_caption(result: MediaResult, decision: SpoilerDecisionSet) -> str:
    """Spoiler işaretlenen içeriklerde anime adını açıklamanın başına ekle."""
    if not any(decision.flags):
        return result.caption[:1024] if result.caption else ""

    titles: list[str] = []
    seen_titles: set[str] = set()
    if decision.assessments:
        for index, has_spoiler in enumerate(decision.flags):
            if not has_spoiler or index >= len(decision.assessments):
                continue
            title = decision.assessments[index].anime_title
            if not title:
                continue
            title = title.strip()
            normalized = title.casefold()
            if title and normalized not in seen_titles:
                titles.append(title)
                seen_titles.add(normalized)

    anime_names = ", ".join(titles) if titles else "Bilinmeyen anime"
    header = f"⚠️ Spoiler — {anime_names}"
    if not result.caption:
        return header[:1024]
    return f"{header}\n\n{result.caption}"[:1024]


async def send_video(
    update: Update,
    result: MediaResult,
    has_spoiler: bool = False,
    caption: Optional[str] = None,
) -> None:
    message = update.effective_message
    if not message:
        return
    with open(result.files[0], "rb") as video_file:
        await message.reply_video(
            video=video_file,
            caption=caption if caption is not None else result.caption[:1024] or None,
            has_spoiler=has_spoiler,
            reply_to_message_id=message.message_id,
            read_timeout=120,
            write_timeout=120,
            connect_timeout=30,
        )
    logger.info("Video gönderildi (spoiler=%s)", has_spoiler)


async def send_images(
    update: Update,
    result: MediaResult,
    spoiler_flags: Optional[list[bool]] = None,
    caption: Optional[str] = None,
) -> None:
    """Albümde her resim için bağımsız spoiler bayrağı uygula."""
    message = update.effective_message
    if not message:
        return
    spoiler_flags = spoiler_flags or [False] * len(result.files)

    for start in range(0, len(result.files), TELEGRAM_ALBUM_LIMIT):
        chunk = result.files[start:start + TELEGRAM_ALBUM_LIMIT]
        media_group = []
        open_files = []
        for idx, filepath in enumerate(chunk):
            fp = open(filepath, "rb")  # noqa: SIM115
            open_files.append(fp)
            media_group.append(
                InputMediaPhoto(
                    media=fp,
                    has_spoiler=spoiler_flags[start + idx],
                    caption=(
                        (caption if caption is not None else result.caption[:1024] or None)
                        if start == 0 and idx == 0
                        else None
                    ),
                )
            )
        try:
            await message.reply_media_group(
                media=media_group,
                reply_to_message_id=message.message_id,
                read_timeout=120,
                write_timeout=120,
                connect_timeout=30,
            )
            logger.info(
                "Resim albümü gönderildi (%d adet, spoiler=%d)",
                len(media_group),
                sum(spoiler_flags[start:start + len(chunk)]),
            )
        finally:
            for fp in open_files:
                fp.close()


@dataclass
class MediaJob:
    update: Update
    url: str


async def _analyze_result(
    result: MediaResult,
    analyzer: Optional[OpenAISpoilerAnalyzer],
) -> SpoilerDecisionSet:
    count = 1 if result.media_type == "video" else len(result.files)
    if not AI_SPOILER_ENABLED:
        return SpoilerDecisionSet(flags=[False] * count, analyzed=False)
    if analyzer is None:
        return OpenAISpoilerAnalyzer.fallback(count)

    try:
        if result.media_type == "video":
            return await analyzer.analyze_video(
                result.files[0], result.work_dir, result.caption
            )
        return await analyzer.analyze_images(
            result.files, result.work_dir, result.caption
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("AI spoiler analizi başarısız; fallback uygulanıyor: %s", exc)
        return OpenAISpoilerAnalyzer.fallback(count)


async def _process_job(
    job: MediaJob,
    analyzer: Optional[OpenAISpoilerAnalyzer],
    executor: ThreadPoolExecutor,
) -> None:
    result: Optional[MediaResult] = None
    try:
        logger.info("Link işleniyor: %s", job.url)
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(executor, download_media, job.url)
        if result is None:
            logger.warning("Medya indirilemedi: %s", job.url)
            return

        decision = await _analyze_result(result, analyzer)
        caption = build_media_caption(result, decision)
        message = job.update.effective_message
        if not message:
            return

        if result.media_type == "video":
            await send_video(job.update, result, decision.flags[0], caption)
        elif result.media_type == "images":
            if len(result.files) == 1:
                with open(result.files[0], "rb") as photo:
                    await message.reply_photo(
                        photo=photo,
                        caption=caption or None,
                        has_spoiler=decision.flags[0],
                        reply_to_message_id=message.message_id,
                    )
                logger.info("Tek resim gönderildi (spoiler=%s)", decision.flags[0])
            else:
                await send_images(job.update, result, decision.flags, caption)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("Medya işleme/gönderim hatası: %s", exc)
    finally:
        if result:
            if result.work_dir:
                cleanup(result.work_dir)
            else:
                for filepath in result.files:
                    cleanup(filepath)


async def _media_worker(application: Application, worker_id: int) -> None:
    queue: asyncio.Queue = application.bot_data["media_queue"]
    analyzer = application.bot_data.get("spoiler_analyzer")
    executor = application.bot_data["download_executor"]
    logger.info("Medya worker %d başlatıldı", worker_id)
    while True:
        job = await queue.get()
        try:
            await _process_job(job, analyzer, executor)
        finally:
            queue.task_done()


async def on_startup(application: Application) -> None:
    application.bot_data["media_queue"] = asyncio.Queue(
        maxsize=MEDIA_QUEUE_CAPACITY
    )
    application.bot_data["download_executor"] = ThreadPoolExecutor(
        max_workers=AI_JOB_CONCURRENCY,
        thread_name_prefix="media-download",
    )

    analyzer: Optional[OpenAISpoilerAnalyzer] = None
    if AI_SPOILER_ENABLED and OPENAI_API_KEY:
        analyzer = OpenAISpoilerAnalyzer()
        logger.info("AI spoiler tespiti etkin")
    elif AI_SPOILER_ENABLED:
        logger.warning(
            "AI spoiler tespiti etkin fakat OPENAI_API_KEY eksik; fallback=%s",
            AI_FAILURE_POLICY,
        )
    else:
        logger.info("AI spoiler tespiti kapalı")
    application.bot_data["spoiler_analyzer"] = analyzer

    application.bot_data["media_workers"] = [
        asyncio.create_task(
            _media_worker(application, worker_id),
            name=f"media-worker-{worker_id}",
        )
        for worker_id in range(1, AI_JOB_CONCURRENCY + 1)
    ]


async def on_shutdown(application: Application) -> None:
    workers = application.bot_data.get("media_workers", [])
    for worker in workers:
        worker.cancel()
    if workers:
        await asyncio.gather(*workers, return_exceptions=True)
    executor = application.bot_data.get("download_executor")
    if executor:
        executor.shutdown(wait=False, cancel_futures=True)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """URL'leri bul ve ana Telegram handler'ini bekletmeden kuyruğa ekle."""
    message = update.effective_message
    if not message or not message.text:
        return
    urls = extract_urls(message.text)
    if not urls:
        return

    queue: asyncio.Queue = context.application.bot_data["media_queue"]
    for url in urls:
        if seen_recently(url):
            logger.info("Link az önce işlendi, atlanıyor: %s", url)
            continue
        logger.info("Link algılandı, kuyruğa ekleniyor: %s", url)
        await queue.put(MediaJob(update=update, url=url))


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN bulunamadı! .env dosyasını kontrol edin.")
        return

    logger.info("AyumuChanBot başlatılıyor…")
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot çalışıyor! Durdurmak için Ctrl+C")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
