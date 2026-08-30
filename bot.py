#!/usr/bin/env python3
"""AyumuChanBot — Telegram media downloader with automatic spoiler detection."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import random
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    MessageEntity,
    Poll,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PollAnswerHandler,
    filters,
)

from config import (
    ANIME_TITLE_MIN_CONFIDENCE,
    AI_FAILURE_POLICY,
    AI_JOB_CONCURRENCY,
    AI_PROVIDER,
    AI_SPOILER_ENABLED,
    ACTIVITY_ENABLED,
    ACTIVITY_QUIZ_DURATION_SECONDS,
    ACTIVITY_QUIZ_ENABLED,
    ACTIVITY_OWNER_USER_ID,
    ACTIVITY_TIMEZONE,
    ACTIVITY_WEEKLY_REPORT_DAY,
    ACTIVITY_WEEKLY_REPORT_HOUR,
    GROQ_API_KEY,
    INSTAGRAM_PATTERN,
    MEDIA_QUEUE_CAPACITY,
    OPENAI_API_KEY,
    SPOILER_VOTE_FILE,
    TELEGRAM_ALBUM_LIMIT,
    TELEGRAM_BOT_TOKEN,
    X_PATTERN,
)
from downloader import MediaResult, cleanup, download_media
from spoiler_analyzer import OpenAISpoilerAnalyzer, SpoilerDecisionSet
import activity
import quiz

DESCRIPTION_MAX_LENGTH = 100

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
        for secret in (TELEGRAM_BOT_TOKEN, OPENAI_API_KEY, GROQ_API_KEY):
            if secret:
                message = message.replace(secret, "[REDACTED]")
        record.msg = message
        record.args = ()
        return True


@dataclass
class SpoilerVote:
    chat_id: int
    source_message_id: int
    media_type: str
    file_id: str
    caption: str
    voters: set[int]
    created_at: float
    applied: bool = False
    last_voter_id: Optional[int] = None
    source_url: str = ""
    # Oy eşiğine ulaşınca medyanın hangi duruma getirileceği. Eski kayıtlar
    # yalnızca spoiler bildirme akışından geldiği için varsayılan True'dur.
    desired_spoiler: bool = True


_spoiler_votes: dict[str, SpoilerVote] = {}


def _save_spoiler_votes() -> None:
    directory = os.path.dirname(SPOILER_VOTE_FILE)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temporary = f"{SPOILER_VOTE_FILE}.tmp"
    with open(temporary, "w", encoding="utf-8") as vote_file:
        json.dump(
            {
                token: {
                    "chat_id": vote.chat_id,
                    "source_message_id": vote.source_message_id,
                    "media_type": vote.media_type,
                    "file_id": vote.file_id,
                    "caption": vote.caption,
                    "voters": sorted(vote.voters),
                    "created_at": vote.created_at,
                    "applied": vote.applied,
                    "last_voter_id": vote.last_voter_id,
                    "source_url": vote.source_url,
                    "desired_spoiler": vote.desired_spoiler,
                }
                for token, vote in _spoiler_votes.items()
            },
            vote_file,
            ensure_ascii=False,
        )
    os.replace(temporary, SPOILER_VOTE_FILE)


def _load_spoiler_votes() -> None:
    try:
        with open(SPOILER_VOTE_FILE, encoding="utf-8") as vote_file:
            stored = json.load(vote_file)
    except (OSError, ValueError):
        return
    for token, data in stored.items():
        try:
            _spoiler_votes[token] = SpoilerVote(
                chat_id=int(data["chat_id"]),
                source_message_id=int(data["source_message_id"]),
                media_type=str(data["media_type"]),
                file_id=str(data["file_id"]),
                caption=str(data.get("caption", "")),
                voters={int(user_id) for user_id in data.get("voters", [])},
                created_at=float(data["created_at"]),
                applied=bool(data.get("applied", False)),
                last_voter_id=(
                    int(data["last_voter_id"])
                    if data.get("last_voter_id") is not None
                    else None
                ),
                source_url=str(data.get("source_url", "")),
                desired_spoiler=bool(data.get("desired_spoiler", True)),
            )
        except (KeyError, TypeError, ValueError):
            continue


_load_spoiler_votes()


def _new_spoiler_vote(
    source_message: object,
    media_type: str,
    caption: str,
    desired_spoiler: bool = True,
    source_message_id: Optional[int] = None,
    source_url: str = "",
) -> str:
    token = uuid.uuid4().hex[:16]
    _spoiler_votes[token] = SpoilerVote(
        chat_id=source_message.chat_id,
        source_message_id=(
            source_message_id
            if source_message_id is not None
            else source_message.message_id
        ),
        media_type=media_type,
        file_id="",
        caption=caption,
        voters=set(),
        created_at=time.monotonic(),
        desired_spoiler=desired_spoiler,
        source_url=source_url,
    )
    _save_spoiler_votes()
    return token


def _spoiler_vote_markup(
    token: str, count: int = 0, desired_spoiler: bool = True
) -> InlineKeyboardMarkup:
    label = "⚠️ Spoiler bildir" if desired_spoiler else "✅ Spoiler değil"
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(
            f"{label} ({count}/3)",
            callback_data=f"spoiler_vote:{token}",
        )]]
    )


def _caption_for_spoiler_state(caption: str, has_spoiler: bool) -> str:
    """Oy sonrası caption'ı yeni spoiler durumuyla uyumlu hale getir."""
    caption = caption or ""
    if has_spoiler:
        if caption.startswith("⚠️ Spoiler"):
            header, separator, description = caption.partition("\n\n")
            if not separator:
                return header[:1024]
            description = _limit_description(description)
            return f"{header}\n\n{description}"[:1024] if description else header
        description = _limit_description(caption)
        return (
            f"⚠️ Spoiler\n\n{description}"[:1024]
            if description
            else "⚠️ Spoiler"
        )

    if not caption.startswith("⚠️ Spoiler"):
        return _limit_description(caption)
    _, separator, description = caption.partition("\n\n")
    return _limit_description(description) if separator else ""


def _spoiler_caption_entities(caption: str) -> list[MessageEntity]:
    """Caption açıklamasını Telegram'ın metin spoiler entity'siyle kapat."""
    if not caption:
        return []

    # Genel uyarı görünür kalsın; başlıktaki devamı ve açıklama kapatılsın.
    if caption.startswith("⚠️ Spoiler"):
        first_newline = caption.find("\n")
        if first_newline < 0:
            return []
        start = first_newline + 1
        while start < len(caption) and caption[start] == "\n":
            start += 1
    else:
        start = 0

    hidden_text = caption[start:]
    if not hidden_text:
        return []
    offset = len(caption[:start].encode("utf-16-le")) // 2
    length = len(hidden_text.encode("utf-16-le")) // 2
    return [MessageEntity(MessageEntity.SPOILER, offset, length)]


def _bind_spoiler_vote(token: str, sent_message: object) -> None:
    vote = _spoiler_votes.get(token)
    if not vote:
        return
    media = getattr(sent_message, vote.media_type, None)
    if vote.media_type == "video":
        vote.file_id = getattr(media, "file_id", "")
    else:
        photos = getattr(sent_message, "photo", [])
        vote.file_id = getattr(photos[-1], "file_id", "") if photos else ""
    if not vote.file_id:
        _spoiler_votes.pop(token, None)
    _save_spoiler_votes()


for _handler in logging.getLogger().handlers:
    _handler.addFilter(_SensitiveDataFilter())


def extract_urls(text: str) -> list[str]:
    """Mesajdan Instagram ve X URL'lerini çıkar."""
    urls: list[str] = []
    for pattern in (INSTAGRAM_PATTERN, X_PATTERN):
        urls.extend(pattern.findall(text))
    return urls


RECENT_URL_TTL = 180
_recent_urls: dict[tuple[int, str], float] = {}


def seen_recently(url: str, chat_id: int) -> bool:
    """Link aynı sohbette son RECENT_URL_TTL saniyede işlendiyse True."""
    key = (chat_id, url.split("?")[0])
    now = time.monotonic()
    for old_key, seen_at in list(_recent_urls.items()):
        if now - seen_at > RECENT_URL_TTL:
            del _recent_urls[old_key]
    if key in _recent_urls:
        return True
    _recent_urls[key] = now
    return False


def _limit_description(description: str) -> str:
    """Açıklamayı en fazla 100 karaktere indir ve kesildiğini belirt."""
    description = description or ""
    if len(description) <= DESCRIPTION_MAX_LENGTH:
        return description
    return description[: DESCRIPTION_MAX_LENGTH - 1] + "…"


def build_media_caption(result: MediaResult, decision: SpoilerDecisionSet) -> str:
    """Spoilerli medyada açıklayıcı caption'ı metin spoiler entity'si için hazırla."""
    if not any(decision.flags):
        return _limit_description(result.caption)

    titles: list[str] = []
    seen_titles: set[str] = set()
    if decision.assessments:
        for index, has_spoiler in enumerate(decision.flags):
            if not has_spoiler or index >= len(decision.assessments):
                continue
            title = decision.assessments[index].anime_title
            assessment = decision.assessments[index]
            if (
                not title
                or assessment.anime_confidence < ANIME_TITLE_MIN_CONFIDENCE
            ):
                continue
            title = title.strip()
            normalized = title.casefold()
            if title and normalized not in seen_titles:
                titles.append(title)
                seen_titles.add(normalized)

    header = f"⚠️ Spoiler — {', '.join(titles)}" if titles else "⚠️ Spoiler"
    description = _limit_description(result.caption)
    if not description:
        return header
    return f"{header}\n\n{description}"[:1024]


async def send_video(
    update: Update,
    result: MediaResult,
    has_spoiler: bool = False,
    caption: Optional[str] = None,
    source_url: str = "",
) -> None:
    message = update.effective_message
    if not message:
        return
    sent_caption = _caption_for_spoiler_state(
        caption if caption is not None else result.caption,
        has_spoiler,
    )
    vote_token = _new_spoiler_vote(
        message,
        "video",
        sent_caption,
        desired_spoiler=not has_spoiler,
        source_url=source_url,
    )
    with open(result.files[0], "rb") as video_file:
        sent_message = await message.reply_video(
            video=video_file,
            caption=sent_caption or None,
            caption_entities=(
                _spoiler_caption_entities(sent_caption) if has_spoiler else None
            ),
            has_spoiler=has_spoiler,
            reply_markup=_spoiler_vote_markup(
                vote_token,
                desired_spoiler=not has_spoiler,
            ),
            reply_to_message_id=message.message_id,
            read_timeout=120,
            write_timeout=120,
            connect_timeout=30,
        )
    if vote_token and sent_message:
        _bind_spoiler_vote(vote_token, sent_message)
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
    album_has_spoiler = any(spoiler_flags)
    album_caption = _caption_for_spoiler_state(
        caption if caption is not None else result.caption,
        album_has_spoiler,
    )

    for start in range(0, len(result.files), TELEGRAM_ALBUM_LIMIT):
        chunk = result.files[start:start + TELEGRAM_ALBUM_LIMIT]
        media_group = []
        open_files = []
        for idx, filepath in enumerate(chunk):
            fp = open(filepath, "rb")  # noqa: SIM115
            open_files.append(fp)
            media_caption = (
                (album_caption or None)
                if start == 0 and idx == 0
                else None
            )
            media_group.append(
                InputMediaPhoto(
                    media=fp,
                    has_spoiler=spoiler_flags[start + idx],
                    caption=media_caption,
                    caption_entities=(
                        _spoiler_caption_entities(media_caption or "")
                        if media_caption and album_has_spoiler
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

        if ACTIVITY_ENABLED:
            try:
                source_chat = job.update.effective_chat
                source_user = job.update.effective_user
                if source_chat and source_user:
                    activity.touch_group(
                        source_chat.id,
                        source_chat.type,
                        getattr(source_chat, "title", "") or "",
                    )
                    if activity.record_link(
                        source_chat.id,
                        source_chat.type,
                        source_user,
                        job.update.effective_message.message_id
                        if job.update.effective_message
                        else 0,
                        job.url,
                    ):
                        logger.info(
                            "Aktivite puanı verildi (type=link_share user=%s points=2)",
                            source_user.id,
                        )
            except Exception as exc:
                logger.warning("Aktivite link puanı kaydedilemedi: %s", exc)

        decision = await _analyze_result(result, analyzer)
        caption = build_media_caption(result, decision)
        message = job.update.effective_message
        if not message:
            return

        if result.media_type == "video":
            await send_video(
                job.update, result, decision.flags[0], caption, source_url=job.url
            )
        elif result.media_type == "images":
            if len(result.files) == 1:
                vote_token = _new_spoiler_vote(
                    message,
                    "photo",
                    _caption_for_spoiler_state(
                        caption if caption is not None else result.caption,
                        decision.flags[0],
                    ),
                    desired_spoiler=not decision.flags[0],
                    source_url=job.url,
                )
                with open(result.files[0], "rb") as photo:
                    sent_caption = _caption_for_spoiler_state(
                        caption if caption is not None else result.caption,
                        decision.flags[0],
                    )
                    sent_message = await message.reply_photo(
                        photo=photo,
                        caption=sent_caption or None,
                        caption_entities=(
                            _spoiler_caption_entities(sent_caption)
                            if decision.flags[0]
                            else None
                        ),
                        has_spoiler=decision.flags[0],
                        reply_markup=(
                            _spoiler_vote_markup(
                                vote_token,
                                desired_spoiler=not decision.flags[0],
                            )
                        ),
                        reply_to_message_id=message.message_id,
                    )
                if vote_token and sent_message:
                    _bind_spoiler_vote(vote_token, sent_message)
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


def _group_for_update(update: Update):
    chat = update.effective_chat
    return chat if chat and chat.type in {"group", "supergroup"} else None


def _command_group(update: Update):
    """Resolve the group for a command, including private-command fallback."""
    chat = _group_for_update(update)
    if chat:
        return chat.id, chat.type
    user = update.effective_user
    if not user:
        return None
    chat_id = activity.latest_group_id(user.id)
    return (chat_id, "supergroup") if chat_id else None


def _touch_activity(update: Update) -> None:
    if not ACTIVITY_ENABLED:
        return
    chat = _group_for_update(update)
    user = update.effective_user
    if chat:
        activity.touch_group(chat.id, chat.type, getattr(chat, "title", "") or "")
        if user:
            activity.upsert_member(chat.id, user, chat.type)


def _user_label(update: Update) -> str:
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return "Kullanıcı"
    activity.upsert_member(chat.id, user, chat.type)
    return activity.member_label(chat.id, user.id)


async def _send_command_result(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> None:
    """Admin sonucu grubu kirletmeden komutu yazan kişinin DM'ine gönder."""
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user:
        return
    if chat and chat.type == "private":
        await message.reply_text(text, parse_mode="HTML")
        return
    try:
        await context.bot.send_message(
            chat_id=user.id,
            text=text,
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.warning("Admin sonucu DM gönderilemedi (user=%s): %s", user.id, exc)


def _leaderboard_text(chat_id: int, week_only: bool = True) -> str:
    title = "Haftalık liderlik" if week_only else "Tüm zamanlar liderliği"
    lines = [
        f"👑 Yönetici: {activity.owner_label_html()}",
        f"<b>{html.escape(activity.OWNER_TITLE)}</b>",
        "",
        f"🏆 <b>{title}</b>",
    ]
    rows = activity.leaderboard(chat_id, week_only=week_only, limit=10)
    if not rows:
        return "\n".join(lines + ["Henüz puan kazanan yok."])
    medals = ("🥇", "🥈", "🥉")
    for index, row in enumerate(rows):
        rank = medals[index] if index < 3 else f"{index + 1}."
        label = activity.member_label(chat_id, int(row["user_id"]))
        lines.append(f"{rank} {label} — <b>{row['points']} puan</b>")
    return "\n".join(lines)


async def handle_points(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not ACTIVITY_ENABLED:
        return
    _touch_activity(update)
    user = update.effective_user
    group = _command_group(update)
    if not group or not user or not update.effective_message:
        return
    chat_id, _ = group
    row = activity.member_summary(chat_id, user.id)
    total = int(row["total_points"]) if row else 0
    weekly = int(row["week_points"]) if row else 0
    earned = activity.badges(total)
    badge_text = ", ".join(earned) if earned else "Henüz rozet yok"
    await update.effective_message.reply_text(
        f"🎴 {activity.member_label(chat_id, user.id)}\n"
        f"Toplam puan: <b>{total}</b>\n"
        f"Bu hafta: <b>{weekly}</b>\n"
        f"Rozetler: {html.escape(badge_text)}",
        parse_mode="HTML",
    )


async def handle_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not ACTIVITY_ENABLED:
        return
    _touch_activity(update)
    group = _command_group(update)
    if not group or not update.effective_message:
        return
    chat_id, _ = group
    week_only = not context.args or context.args[0].lower() not in {"all", "toplam"}
    await update.effective_message.reply_text(
        _leaderboard_text(chat_id, week_only=week_only), parse_mode="HTML"
    )


async def handle_badges(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not ACTIVITY_ENABLED:
        return
    _touch_activity(update)
    user = update.effective_user
    group = _command_group(update)
    if not group or not user or not update.effective_message:
        return
    chat_id, _ = group
    row = activity.member_summary(chat_id, user.id)
    total = int(row["total_points"]) if row else 0
    earned = activity.badges(total)
    text = "🏅 <b>Rozetlerin</b>\n" + (
        "\n".join(f"• {html.escape(badge)}" for badge in earned)
        if earned
        else "Henüz rozet kazanmadın."
    )
    await update.effective_message.reply_text(text, parse_mode="HTML")


def _statistics_text(chat_id: int, user_id: int) -> str:
    row = activity.member_summary(chat_id, user_id)
    stats = activity.statistics(chat_id, user_id)
    total = int(row["total_points"]) if row else 0
    weekly = int(row["week_points"]) if row else 0
    return (
        f"📊 <b>{activity.member_label(chat_id, user_id)} istatistikleri</b>\n"
        f"Toplam puan: <b>{total}</b>\n"
        f"Bu hafta: <b>{weekly}</b>\n"
        f"Link: {stats.get('link_share', 0)}\n"
        f"Spoiler oyu: {stats.get('spoiler_vote', 0)}\n"
        f"Öneri: {stats.get('recommendation', 0)}\n"
        f"Quiz doğru cevabı: {stats.get('quiz_correct', 0)}\n"
        f"Rozet düzeltme bonusu: {stats.get('spoiler_resolution', 0)}\n"
        f"Ceza: {stats.get('penalty', 0)}"
    )


def _canonical_media_url(url: str) -> str:
    return url.split("?", 1)[0].rstrip("/")


def _spoiler_votes_for_url(url: str) -> list[SpoilerVote]:
    """Resolve current and legacy spoiler records for one source URL."""
    canonical = _canonical_media_url(url)
    matches = [
        vote
        for vote in _spoiler_votes.values()
        if vote.source_url and _canonical_media_url(vote.source_url) == canonical
    ]
    known_source_messages = set(activity.link_message_sources(url))
    for vote in _spoiler_votes.values():
        if (vote.chat_id, vote.source_message_id) in known_source_messages and vote not in matches:
            matches.append(vote)
    return sorted(matches, key=lambda vote: vote.created_at)


def _spoiler_vote_report(url: str) -> str:
    votes = _spoiler_votes_for_url(url)
    if not votes:
        return "🔎 Bu link için kayıtlı spoiler oyu bulunamadı."
    spoiler_users: set[int] = set()
    non_spoiler_users: set[int] = set()
    for vote in votes:
        target = spoiler_users if vote.desired_spoiler else non_spoiler_users
        target.update(vote.voters)

    def labels(user_ids: set[int], chat_id: int) -> str:
        return ", ".join(activity.member_label(chat_id, user_id) for user_id in sorted(user_ids)) or "Yok"

    chat_id = votes[0].chat_id
    lines = [
        "🔎 <b>Spoiler oyları</b>",
        f"Link: {html.escape(url)}",
        "",
        f"⚠️ <b>Spoiler diyenler:</b> {labels(spoiler_users, chat_id)}",
        f"✅ <b>Spoiler değil diyenler:</b> {labels(non_spoiler_users, chat_id)}",
        f"📊 Oylama turu: {len(votes)}",
    ]
    return "\n".join(lines)


async def handle_spoiler_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    user = update.effective_user
    if not user or not activity.is_owner(user.id):
        return
    if not context.args:
        await _send_command_result(update, context, "Kullanım: /spoiler <Instagram/X linki>")
        return
    urls = extract_urls(" ".join(context.args))
    if not urls:
        await _send_command_result(update, context, "Geçerli bir Instagram veya X linki bulunamadı.")
        return
    await _send_command_result(update, context, _spoiler_vote_report(urls[0]))


async def handle_statistics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not ACTIVITY_ENABLED:
        return
    _touch_activity(update)
    user = update.effective_user
    group = _command_group(update)
    if not group or not user or not update.effective_message:
        return
    chat_id, _ = group
    if context.args:
        if not activity.is_owner(user.id):
            await _send_command_result(
                update, context, "Başkasının istatistiğini yalnızca yönetici görebilir."
            )
            return
        if len(context.args) != 1:
            await _send_command_result(
                update, context, "Kullanım: /istatistik @username"
            )
            return
        target = activity.find_member(chat_id, context.args[0])
        if not target:
            await _send_command_result(update, context, "Kullanıcı bulunamadı.")
            return
        await _send_command_result(
            update, context, _statistics_text(chat_id, int(target["user_id"]))
        )
        return
    await update.effective_message.reply_text(
        _statistics_text(chat_id, user.id), parse_mode="HTML"
    )


async def handle_recommendation(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not ACTIVITY_ENABLED:
        return
    _touch_activity(update)
    user = update.effective_user
    message = update.effective_message
    group = _command_group(update)
    if not group or not user or not message:
        return
    chat_id, chat_type = group
    recommendation = " ".join(context.args).strip()
    if not recommendation:
        await message.reply_text("Kullanım: /oneri Anime adı — kısa neden")
        return
    recommendation = recommendation[:200]
    recommendation_status = activity.recommendation_status(
        chat_id, chat_type, user, message.message_id, recommendation
    )
    if recommendation_status == "duplicate":
        logger.info(
            "Öneri mesajı daha önce işlendi, tekrar yanıtlanmadı (chat=%s message=%s)",
            chat_id,
            message.message_id,
        )
        return
    suffix = (
        " (+2 puan)"
        if recommendation_status == "recorded"
        else " (haftalık öneri limitin doldu)"
    )
    await message.reply_text(
        f"🎬 {activity.member_label(chat_id, user.id)} öneriyor:\n"
        f"{html.escape(recommendation)}{suffix}",
        parse_mode="HTML",
    )


async def handle_admin_adjustment(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    user = update.effective_user
    group = _command_group(update)
    if not user or not activity.is_owner(user.id) or not group:
        return
    chat_id, _ = group
    if len(context.args) < 2:
        await _send_command_result(
            update, context, "Kullanım: /puanazalt @username miktar sebep"
        )
        return
    target = activity.find_member(chat_id, context.args[0])
    try:
        amount = abs(int(context.args[1]))
    except ValueError:
        amount = 0
    reason = " ".join(context.args[2:]).strip() or "Yönetici düzeltmesi"
    if not target or not amount:
        await _send_command_result(update, context, "Kullanıcı veya miktar geçersiz.")
        return
    points = -amount if update.effective_message.text.startswith("/puanazalt") else amount
    applied = activity.manual_adjust(
        chat_id, int(target["user_id"]), points, reason, user.id
    )
    await _send_command_result(
        update,
        context,
        f"{'✅' if applied else '❌'} {activity.member_label(chat_id, int(target['user_id']))} "
        f"için {points:+d} puan işlendi.<br>Sebep: {html.escape(reason)}",
    )


async def handle_admin_history(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    user = update.effective_user
    group = _command_group(update)
    if not user or not activity.is_owner(user.id) or not group:
        return
    chat_id, _ = group
    if not context.args:
        await _send_command_result(update, context, "Kullanım: /cezagecmisi @username")
        return
    target = activity.find_member(chat_id, context.args[0])
    if not target:
        await _send_command_result(update, context, "Kullanıcı bulunamadı.")
        return
    history = activity.penalty_history(chat_id, int(target["user_id"]))
    if not history:
        await _send_command_result(update, context, "Bu kullanıcı için ceza kaydı yok.")
        return
    lines = [f"⚖️ <b>{activity.member_label(chat_id, int(target['user_id']))} ceza geçmişi</b>"]
    for row in history:
        status = {"approved": "uygulandı", "rejected": "reddedildi", "pending": "bekliyor"}.get(
            row["status"], row["status"]
        )
        lines.append(
            f"#{row['id']} — {status} — {row['points_delta']} puan — "
            f"{html.escape(row['reason'] or 'Sebep belirtilmedi')}"
        )
    await _send_command_result(
        update,
        context,
        "\n".join(lines),
    )


async def handle_pending_penalties(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    user = update.effective_user
    if not user or not activity.is_owner(user.id):
        return
    rows = activity.pending_reviews()
    if not rows:
        await _send_command_result(update, context, "Bekleyen ceza incelemesi yok.")
        return
    lines = ["⚖️ <b>Bekleyen ceza incelemeleri</b>"]
    for row in rows:
        lines.append(
            f"#{row['id']} — {activity.member_label(row['chat_id'], row['user_id'])} "
            f"({row['points_delta']} puan)"
        )
    await _send_command_result(update, context, "\n".join(lines))


async def handle_command_trace(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Log every command, including commands not registered by the bot."""
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    logger.info(
        "Komut alındı (text=%s user=%s chat=%s type=%s)",
        (message.text or "")[:120] if message else "-",
        user.id if user else "-",
        chat.id if chat else "-",
        chat.type if chat else "-",
    )


async def handle_error(
    update: object, context: ContextTypes.DEFAULT_TYPE
) -> None:
    logger.error("Telegram güncellemesi işlenirken hata oluştu", exc_info=context.error)


async def handle_poll_answer(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    answer = update.poll_answer
    if not answer or not answer.poll_id or not answer.user:
        return
    awarded = activity.record_quiz_answer(
        answer.poll_id, answer.user, answer.option_ids
    )
    logger.info(
        "Quiz cevabı alındı (poll=%s user=%s doğru=%s)",
        answer.poll_id,
        answer.user.id,
        awarded,
    )


async def _delete_quiz_after(
    application: Application, chat_id: int, message_id: int,
    poll_id: str, delay: float,
) -> None:
    await asyncio.sleep(max(1, delay))
    try:
        await application.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as exc:
        logger.warning("Quiz mesajı silinemedi (chat=%s message=%s): %s", chat_id, message_id, exc)
    finally:
        activity.mark_quiz_deleted(poll_id)


async def _cleanup_expired_quizzes(application: Application) -> None:
    for row in activity.expired_quizzes():
        try:
            await application.bot.delete_message(
                chat_id=int(row["chat_id"]), message_id=int(row["message_id"])
            )
        except Exception as exc:
            logger.warning(
                "Süresi geçmiş quiz silinemedi (chat=%s message=%s): %s",
                row["chat_id"], row["message_id"], exc,
            )
        finally:
            activity.mark_quiz_deleted(str(row["poll_id"]))


async def _send_scheduled_quiz(application: Application, slot_key: str) -> None:
    if not activity.claim_quiz_slot(slot_key):
        return
    question = quiz.random_question()
    options = list(question.options)
    correct_answer = options[question.correct_option_id]
    random.shuffle(options)
    correct_option_id = options.index(correct_answer)
    for chat_id in activity.quiz_group_ids():
        try:
            activity.touch_group(chat_id, "supergroup", "")
            message = await application.bot.send_poll(
                chat_id=chat_id,
                question=f"🧠 Anime Quiz\n\n{question.question}",
                options=options,
                type=Poll.QUIZ,
                correct_option_id=correct_option_id,
                is_anonymous=False,
                allows_multiple_answers=False,
                open_period=ACTIVITY_QUIZ_DURATION_SECONDS,
            )
            expires_at = time.time() + ACTIVITY_QUIZ_DURATION_SECONDS
            activity.register_quiz(
                message.poll.id,
                chat_id,
                message.message_id,
                correct_option_id,
                expires_at,
            )
            asyncio.create_task(
                _delete_quiz_after(
                    application,
                    chat_id,
                    message.message_id,
                    message.poll.id,
                    ACTIVITY_QUIZ_DURATION_SECONDS,
                )
            )
            logger.info(
                "Quiz gönderildi (slot=%s chat=%s poll=%s)",
                slot_key, chat_id, message.poll.id,
            )
        except Exception as exc:
            logger.warning("Quiz gönderilemedi (slot=%s chat=%s): %s", slot_key, chat_id, exc)


async def _quiz_scheduler(application: Application) -> None:
    while True:
        await _cleanup_expired_quizzes(application)
        activity.ensure_quiz_schedule()
        slot = activity.upcoming_quiz_slot()
        if not slot:
            await asyncio.sleep(15 * 60)
            continue
        delay = max(1, float(slot["scheduled_at"]) - time.time())
        await asyncio.sleep(delay)
        await _send_scheduled_quiz(application, str(slot["slot_key"]))


async def _weekly_report_loop(application: Application) -> None:
    try:
        zone = ZoneInfo(ACTIVITY_TIMEZONE)
    except Exception:
        zone = ZoneInfo("UTC")
    while True:
        now = datetime.now(zone)
        days = (ACTIVITY_WEEKLY_REPORT_DAY - now.weekday()) % 7
        target = now.replace(
            hour=ACTIVITY_WEEKLY_REPORT_HOUR,
            minute=0,
            second=0,
            microsecond=0,
        ) + timedelta(days=days)
        if target <= now:
            target += timedelta(days=7)
        await asyncio.sleep(max(1, (target - now).total_seconds()))
        for chat_id in activity.group_ids():
            try:
                await application.bot.send_message(
                    chat_id=chat_id,
                    text=activity.report_html(chat_id),
                    parse_mode="HTML",
                )
            except Exception as exc:
                logger.warning("Haftalık aktivite raporu gönderilemedi (chat=%s): %s", chat_id, exc)


async def _notify_penalty_reviews(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    vote_token: str,
    voter_ids: set[int],
) -> None:
    if not ACTIVITY_ENABLED or not ACTIVITY_OWNER_USER_ID:
        return
    eligible_users = {
        user_id for user_id in voter_ids if not activity.is_owner(user_id)
    }
    review_ids = activity.create_penalty_reviews(chat_id, vote_token, eligible_users)
    if not review_ids:
        return
    lines = [
        "⚖️ <b>Spoiler oyu incelemesi</b>",
        "Bu oyların hatalı kullanım olup olmadığına yalnızca sen karar verebilirsin.",
        "",
    ]
    for review_id, user_id in zip(review_ids, sorted(eligible_users)):
        lines.append(f"{activity.member_label(chat_id, user_id)} — inceleme #{review_id}")
    keyboard = []
    for review_id in review_ids:
        keyboard.append([
            InlineKeyboardButton("✅ -2 uygula", callback_data=f"activity_penalty:approve:{review_id}"),
            InlineKeyboardButton("❌ Ceza verme", callback_data=f"activity_penalty:reject:{review_id}"),
        ])
    try:
        await context.bot.send_message(
            chat_id=ACTIVITY_OWNER_USER_ID,
            text="\n".join(lines),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception as exc:
        logger.warning("Ceza inceleme DM'i gönderilemedi: %s", exc)


async def handle_penalty_review(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if not query or not query.data or not query.from_user:
        return
    if not activity.is_owner(query.from_user.id):
        await query.answer("Bu inceleme yalnızca yöneticinin yetkisinde.", show_alert=True)
        return
    try:
        _, action, review_text = query.data.split(":", 2)
        review_id = int(review_text)
    except (ValueError, AttributeError):
        await query.answer("Geçersiz inceleme.", show_alert=True)
        return
    if action == "approve":
        changed = activity.apply_penalty(review_id, query.from_user.id)
        answer = "Ceza uygulandı." if changed else "Bu ceza zaten işlendi veya geçersiz."
    else:
        changed = activity.reject_penalty(review_id, query.from_user.id)
        answer = "Ceza iptal edildi." if changed else "Bu inceleme zaten sonuçlanmış."
    await query.answer(answer)
    if query.message:
        current_markup = query.message.reply_markup
        remaining_rows = []
        if current_markup:
            for row in current_markup.inline_keyboard:
                remaining = [
                    button
                    for button in row
                    if str(getattr(button, "callback_data", "")).rsplit(":", 1)[-1]
                    != str(review_id)
                ]
                if remaining:
                    remaining_rows.append(remaining)
        await query.edit_message_reply_markup(
            reply_markup=(InlineKeyboardMarkup(remaining_rows) if remaining_rows else None)
        )


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
    provider_key = GROQ_API_KEY if AI_PROVIDER == "groq" else OPENAI_API_KEY
    if AI_SPOILER_ENABLED and provider_key:
        analyzer = OpenAISpoilerAnalyzer()
        logger.info("AI spoiler tespiti etkin (provider=%s)", AI_PROVIDER)
    elif AI_SPOILER_ENABLED:
        logger.warning(
            "AI spoiler tespiti etkin fakat %s anahtarı eksik; fallback=%s",
            AI_PROVIDER,
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
    if ACTIVITY_ENABLED:
        # Schema is created lazily by the activity module; this call also
        # discovers groups already seen before a restart.
        activity.group_ids()
        application.bot_data["activity_report_task"] = asyncio.create_task(
            _weekly_report_loop(application), name="activity-weekly-report"
        )
        if ACTIVITY_QUIZ_ENABLED:
            activity.ensure_quiz_schedule()
            application.bot_data["activity_quiz_task"] = asyncio.create_task(
                _quiz_scheduler(application), name="activity-quiz-scheduler"
            )


async def on_shutdown(application: Application) -> None:
    workers = application.bot_data.get("media_workers", [])
    for worker in workers:
        worker.cancel()
    if workers:
        await asyncio.gather(*workers, return_exceptions=True)
    report_task = application.bot_data.get("activity_report_task")
    if report_task:
        report_task.cancel()
        await asyncio.gather(report_task, return_exceptions=True)
    quiz_task = application.bot_data.get("activity_quiz_task")
    if quiz_task:
        quiz_task.cancel()
        await asyncio.gather(quiz_task, return_exceptions=True)
    executor = application.bot_data.get("download_executor")
    if executor:
        executor.shutdown(wait=False, cancel_futures=True)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """URL'leri bul ve ana Telegram handler'ini bekletmeden kuyruğa ekle."""
    # Reactions and edited messages can expose the original message through
    # effective_message. They must never re-trigger a command or URL job.
    message = update.message
    if not message or not message.text:
        return
    _touch_activity(update)
    urls = extract_urls(message.text)
    if not urls:
        return

    queue: asyncio.Queue = context.application.bot_data["media_queue"]
    for url in urls:
        if seen_recently(url, message.chat_id):
            logger.info("Link az önce işlendi, atlanıyor: %s", url)
            continue
        logger.info("Link algılandı, kuyruğa ekleniyor: %s", url)
        await queue.put(MediaJob(update=update, url=url))


async def handle_spoiler_vote(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Two distinct users can correct a missed spoiler after delivery."""
    query = update.callback_query
    logger.info(
        "Spoiler callback alındı (data=%s user=%s)",
        query.data if query else "-",
        query.from_user.id if query and query.from_user else "-",
    )
    if not query or not query.data:
        return
    token = query.data.removeprefix("spoiler_vote:")
    vote = _spoiler_votes.get(token)
    if not vote:
        # Recover buttons created before a bot restart. Telegram keeps the
        # media file_id on the callback message even though RAM was cleared.
        sent_message = query.message
        source_message = getattr(sent_message, "reply_to_message", None)
        if getattr(sent_message, "video", None):
            vote = SpoilerVote(
                chat_id=sent_message.chat_id,
                source_message_id=(
                    source_message.message_id
                    if source_message
                    else sent_message.message_id
                ),
                media_type="video",
                file_id=sent_message.video.file_id,
                caption=sent_message.caption or "",
                voters=set(),
                created_at=time.monotonic(),
            )
        elif getattr(sent_message, "photo", None):
            vote = SpoilerVote(
                chat_id=sent_message.chat_id,
                source_message_id=(
                    source_message.message_id
                    if source_message
                    else sent_message.message_id
                ),
                media_type="photo",
                file_id=sent_message.photo[-1].file_id,
                caption=sent_message.caption or "",
                voters=set(),
                created_at=time.monotonic(),
            )
        if vote:
            _spoiler_votes[token] = vote
            _save_spoiler_votes()
    if not vote or not vote.file_id:
        await query.answer("Bu spoiler bildirimi artık geçerli değil.", show_alert=True)
        return
    if vote.applied:
        await query.answer("Bu oy zaten uygulandı.")
        return
    user = query.from_user
    if not user or user.id in vote.voters:
        await query.answer("Oyun zaten kayıtlı.")
        return

    vote.voters.add(user.id)
    vote.last_voter_id = user.id
    chat_type = getattr(getattr(query.message, "chat", None), "type", "")
    try:
        if ACTIVITY_ENABLED and activity.record_spoiler_vote(
            vote.chat_id, chat_type, user, token
        ):
            logger.info(
                "Aktivite puanı verildi (type=spoiler_vote user=%s points=1)",
                user.id,
            )
    except Exception as exc:
        logger.warning("Aktivite spoiler oyu kaydedilemedi: %s", exc)
    _save_spoiler_votes()
    if len(vote.voters) < 3:
        await query.answer(f"Spoiler bildirimi kaydedildi ({len(vote.voters)}/3).")
        await query.edit_message_reply_markup(
            reply_markup=_spoiler_vote_markup(
                token,
                len(vote.voters),
                desired_spoiler=vote.desired_spoiler,
            )
        )
        return

    vote.applied = True
    _save_spoiler_votes()
    caption = _caption_for_spoiler_state(vote.caption, vote.desired_spoiler)
    if not query.message:
        vote.applied = False
        _save_spoiler_votes()
        await query.answer("Mesaj güncellenemedi.", show_alert=True)
        return

    replacement_token = _new_spoiler_vote(
        query.message,
        vote.media_type,
        caption,
        desired_spoiler=not vote.desired_spoiler,
        source_message_id=vote.source_message_id,
        source_url=vote.source_url,
    )
    replacement_vote = _spoiler_votes[replacement_token]
    # Aynı Telegram mesajını düzenleyeceğimiz için yeni medya file_id'sini
    # doğrudan taşırız; böylece yeni token restart sonrasında da çalışır.
    replacement_vote.file_id = vote.file_id
    _save_spoiler_votes()
    try:
        if vote.media_type == "video":
            await query.message.edit_media(
                media=InputMediaVideo(
                    media=vote.file_id,
                    caption=caption[:1024] or None,
                    caption_entities=(
                        _spoiler_caption_entities(caption)
                        if vote.desired_spoiler
                        else None
                    ),
                    has_spoiler=vote.desired_spoiler,
                ),
                reply_markup=_spoiler_vote_markup(
                    replacement_token,
                    desired_spoiler=not vote.desired_spoiler,
                ),
            )
        else:
            await query.message.edit_media(
                media=InputMediaPhoto(
                    media=vote.file_id,
                    caption=caption[:1024] or None,
                    caption_entities=(
                        _spoiler_caption_entities(caption)
                        if vote.desired_spoiler
                        else None
                    ),
                    has_spoiler=vote.desired_spoiler,
                ),
                reply_markup=_spoiler_vote_markup(
                    replacement_token,
                    desired_spoiler=not vote.desired_spoiler,
                ),
            )
        try:
            if ACTIVITY_ENABLED:
                activity.record_resolution_bonus(
                    vote.chat_id, chat_type, token, vote.last_voter_id
                )
        except Exception as exc:
            logger.warning("Aktivite çözüm bonusu kaydedilemedi: %s", exc)
        if vote.desired_spoiler:
            await query.answer(
                "3 kişi bildirdi; mevcut mesaj spoiler olarak güncellendi."
            )
        else:
            await query.answer(
                "3 kişi bildirdi; mevcut mesajdaki spoiler kaldırıldı."
            )
        try:
            await _notify_penalty_reviews(
                context, vote.chat_id, token, vote.voters
            )
        except Exception as exc:
            logger.warning("Ceza incelemesi oluşturulamadı: %s", exc)
        logger.info(
            "Topluluk düzeltme oyu uygulandı (chat=%s message=%s desired_spoiler=%s)",
            vote.chat_id,
            vote.source_message_id,
            vote.desired_spoiler,
        )
    except Exception as exc:
        vote.applied = False
        _spoiler_votes.pop(replacement_token, None)
        _save_spoiler_votes()
        logger.warning("Topluluk spoiler oyu uygulanamadı: %s", exc)
        await query.answer("Spoiler düzeltmesi uygulanamadı.", show_alert=True)


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
    # Callback queries must be handled before the text handler and explicitly
    # included in polling updates so community spoiler votes are not dropped.
    app.add_handler(
        CallbackQueryHandler(
            handle_penalty_review, pattern=r"^activity_penalty:"
        ),
        group=-2,
    )
    app.add_handler(
        CallbackQueryHandler(handle_spoiler_vote, pattern=r"^spoiler_vote:"),
        group=-1,
    )
    app.add_handler(PollAnswerHandler(handle_poll_answer), group=-1)
    message_only = filters.UpdateType.MESSAGE
    app.add_handler(CommandHandler("puan", handle_points, filters=message_only))
    app.add_handler(CommandHandler("liderlik", handle_leaderboard, filters=message_only))
    app.add_handler(CommandHandler("rozetler", handle_badges, filters=message_only))
    app.add_handler(CommandHandler("istatistik", handle_statistics, filters=message_only))
    app.add_handler(CommandHandler("spoiler", handle_spoiler_command, filters=message_only))
    app.add_handler(CommandHandler("oneri", handle_recommendation, filters=message_only))
    app.add_handler(
        CommandHandler(["puanazalt", "puanver"], handle_admin_adjustment, filters=message_only)
    )
    app.add_handler(CommandHandler("cezagecmisi", handle_admin_history, filters=message_only))
    app.add_handler(CommandHandler("bekleyencezalar", handle_pending_penalties, filters=message_only))
    # group=1: command handler'ları çalıştıktan sonra teşhis için kayda al.
    app.add_handler(
        MessageHandler(filters.COMMAND & message_only, handle_command_trace), group=1
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(handle_error)
    logger.info("Bot çalışıyor! Durdurmak için Ctrl+C")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
