"""AyumuChanBot — Media Downloader

yt-dlp ile video, instaloader ile Instagram resim, gallery-dl ile X resim indirme.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import threading
import uuid
from dataclasses import dataclass, field
from typing import Optional

import instaloader
import yt_dlp

from config import DOWNLOAD_DIR, MAX_FILE_SIZE, MAX_IMAGES, IG_COOKIES, X_COOKIES

logger = logging.getLogger(__name__)


# ── Veri Yapısı ───────────────────────────────────────────────────────────────

@dataclass
class MediaResult:
    """İndirilen medya sonucu."""
    media_type: str                     # "video" | "images"
    files: list[str] = field(default_factory=list)
    caption: str = ""
    work_dir: str = ""


# ── Yardımcı Fonksiyonlar ────────────────────────────────────────────────────

def _ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def _session_dir() -> str:
    path = os.path.join(DOWNLOAD_DIR, uuid.uuid4().hex[:12])
    return _ensure_dir(path)


def cleanup(path: str) -> None:
    """Geçici dizini/dosyayı sil."""
    try:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


def _check_size(filepath: str) -> bool:
    return os.path.getsize(filepath) <= MAX_FILE_SIZE


def _collect_files(directory: str, extensions: tuple[str, ...]) -> list[str]:
    """Dizindeki belirtilen uzantılı dosyaları sıralı döndür."""
    return sorted(
        os.path.join(directory, f)
        for f in os.listdir(directory)
        if f.lower().endswith(extensions)
    )


# ── Instagram Shortcode Çıkarma ──────────────────────────────────────────────

def _extract_ig_shortcode(url: str) -> Optional[str]:
    """Instagram URL'den shortcode çıkar."""
    m = re.search(r"instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)", url)
    return m.group(1) if m else None


# ── Cookie Dosyaları ─────────────────────────────────────────────────────────

_COOKIEFILES: dict[str, str] = {}
_COOKIEFILE_LOCK = threading.Lock()
_GALLERY_DL_LOCK = threading.Lock()


def _cookiefile(key: str, domains: tuple[str, ...], cookies: dict[str, str]) -> Optional[str]:
    """Cookie'leri Netscape formatında dosyaya yaz — yt-dlp ve gallery-dl için.

    Her iki araç da cookie'yi dosyadan okuyor. Instagram sessionid'yi,
    X ise guest token'ı tek başına kabul etmiyor; tam set gerekiyor.
    """
    cookies = {name: value for name, value in cookies.items() if value}
    if not cookies:
        return None

    with _COOKIEFILE_LOCK:
        path = _COOKIEFILES.get(key)
        if path and os.path.isfile(path):
            return path

        path = os.path.join(_ensure_dir(DOWNLOAD_DIR), f"{key}_cookies.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# Netscape HTTP Cookie File\n")
            for domain in domains:
                for name, value in cookies.items():
                    fh.write(
                        f"{domain}\tTRUE\t/\tTRUE\t2147483647\t{name}\t{value}\n"
                    )
        os.chmod(path, 0o600)

        _COOKIEFILES[key] = path
        return path


def _ig_cookiefile() -> Optional[str]:
    return _cookiefile("ig", (".instagram.com",), IG_COOKIES)


# Platform hesabı kısıtladığında dönen hata izleri. Bu durumda sıradaki
# araçları denemek isteği çoğaltmaktan başka işe yaramıyor — üstelik
# kısıtlamayı derinleştiriyor.
_BLOCKED_HINTS = (
    "http error 400",
    "http error 401",
    "http error 403",
    "http error 429",
    "rate-limit",
    "rate limit",
    "login required",
    "checkpoint",
)


def _looks_blocked(message: str) -> bool:
    """Hata mesajı hesap kısıtlamasına mı işaret ediyor?"""
    lowered = message.lower()
    return any(hint in lowered for hint in _BLOCKED_HINTS)


def _cookiefile_for(url: str) -> Optional[str]:
    """URL'nin platformuna uygun cookie dosyasını döndür."""
    if "instagram.com" in url:
        return _ig_cookiefile()
    if "x.com" in url or "twitter.com" in url:
        return _cookiefile("x", (".x.com", ".twitter.com"), X_COOKIES)
    return None


# ── Instagram İndirme (yt-dlp → instaloader → gallery-dl) ────────────────────

def _download_instagram(url: str) -> Optional[MediaResult]:
    """
    Instagram post/reel/carousel indir.

    Sıra önemli: önce yt-dlp denenir. instaloader'ın kullandığı graphql
    endpoint'ini Instagram sık sık blokluyor (401) — eskiden bu çağrı en başta
    olduğu için hata fırlatıp video indirmeyi de komple engelliyordu.
    instaloader artık yalnızca resim gönderilerinde fallback.
    """
    if not _extract_ig_shortcode(url):
        logger.warning("Instagram shortcode bulunamadı: %s", url)
        return None

    # ── 1. Video: yt-dlp ─────────────────────────────────────────────────
    result, error = _download_video_ytdlp(url, _session_dir())
    if result:
        return result

    if _looks_blocked(error):
        logger.warning(
            "Instagram erişimi kısıtlı, diğer araçlar denenmiyor: %s", error
        )
        return None

    # ── 2. Resim: instaloader, olmazsa gallery-dl ────────────────────────
    logger.info("yt-dlp video bulamadı, resim olarak deneniyor: %s", url)
    return _download_instagram_images(url)


def _download_instagram_images(url: str) -> Optional[MediaResult]:
    """Instagram resim/carousel indir — instaloader, başarısızsa gallery-dl."""
    shortcode = _extract_ig_shortcode(url)
    if not shortcode:
        return None

    session = _session_dir()
    caption = ""

    try:
        L = instaloader.Instaloader(
            download_pictures=True,
            download_videos=False,
            download_video_thumbnails=False,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
            compress_json=False,
            post_metadata_txt_pattern="",
        )
        for name, value in IG_COOKIES.items():
            if value:
                L.context._session.cookies.set(name, value, domain=".instagram.com")

        post = instaloader.Post.from_shortcode(L.context, shortcode)
        caption = post.caption or ""
        if len(caption) > 200:
            caption = caption[:200] + "…"

        L.dirname_pattern = session
        L.filename_pattern = "{shortcode}_{mediaid}"
        L.download_post(post, target="")

    except Exception as exc:
        # graphql blokluysa gallery-dl'e düş
        logger.warning("instaloader başarısız (%s), gallery-dl deneniyor", exc)
        try:
            _run_gallery_dl(url, session)
        except Exception as exc2:
            logger.error("Instagram gallery-dl hatası: %s", exc2)
            cleanup(session)
            return None

    image_files = _collect_files(session, (".jpg", ".jpeg", ".png", ".webp"))
    if not image_files:
        logger.warning("Instagram resim bulunamadı: %s", url)
        cleanup(session)
        return None

    selected = [f for f in image_files[:MAX_IMAGES] if _check_size(f)]
    if not selected:
        logger.warning("Tüm resimler boyut limitini aşıyor")
        cleanup(session)
        return None

    return MediaResult(
        media_type="images", files=selected, caption=caption, work_dir=session
    )


# ── Video İndirme (yt-dlp) ───────────────────────────────────────────────────

def _download_video_ytdlp(
    url: str, session: str, caption: str = ""
) -> tuple[Optional[MediaResult], str]:
    """yt-dlp ile video indir.

    (sonuç, hata_mesajı) döndürür — çağıran taraf hatanın kısıtlamadan mı
    yoksa "bu gönderide video yok"tan mı kaynaklandığını ayırt edebilsin diye.
    """
    opts = {
        "format": "best[filesize<50M]/best",
        "outtmpl": os.path.join(session, "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 3,
        "merge_output_format": "mp4",
    }
    
    cookiefile = _cookiefile_for(url)
    if cookiefile:
        opts["cookiefile"] = cookiefile

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            if not caption:
                info = ydl.extract_info(url, download=False)
                caption = (info or {}).get("title", "")
            ydl.download([url])
    except Exception as exc:
        # Resim gönderilerinde bu normal — çağıran taraf resim akışına düşüyor
        logger.info("yt-dlp video indiremedi: %s", exc)
        cleanup(session)
        return None, str(exc)

    video_files = _collect_files(session, (".mp4", ".webm", ".mov", ".mkv"))
    if not video_files:
        logger.warning("Video dosyası bulunamadı: %s", session)
        cleanup(session)
        return None, ""

    if not _check_size(video_files[0]):
        logger.warning("Video çok büyük (>50MB): %s", video_files[0])
        cleanup(session)
        return None, ""

    return MediaResult(
        media_type="video",
        files=[video_files[0]],
        caption=caption,
        work_dir=session,
    ), ""


# ── gallery-dl (resim indirme) ───────────────────────────────────────────────

def _run_gallery_dl(url: str, session: str) -> None:
    """gallery-dl ile resimleri session dizinine indir."""
    import gallery_dl

    # gallery-dl global config kullanıyor; paralel worker'lar birbirinin
    # hedef dizinini/cookie ayarını ezmesin.
    with _GALLERY_DL_LOCK:
        gallery_dl.config.clear()
        gallery_dl.config.load()
        gallery_dl.config.set(("extractor",), "directory", [])
        gallery_dl.config.set(("extractor",), "filename", "{num:>02}.{extension}")
        gallery_dl.config.set(("extractor",), "base-directory", session)

        cookiefile = _cookiefile_for(url)
        if cookiefile:
            gallery_dl.config.set(("extractor",), "cookies", cookiefile)

        job = gallery_dl.job.DownloadJob(url)
        job.run()


# ── X (Twitter) İndirme ──────────────────────────────────────────────────────

def _download_x(url: str) -> Optional[MediaResult]:
    """
    X/Twitter tweet indir.
    - Video ise: yt-dlp kullan
    - Resim ise: gallery-dl kullan (yt-dlp resim desteklemiyor)
    """
    # ── 1. Önce yt-dlp ile video dene ────────────────────────────────────
    result, error = _download_video_ytdlp(url, _session_dir())
    if result:
        return result

    if _looks_blocked(error):
        logger.warning("X erişimi kısıtlı, gallery-dl denenmiyor: %s", error)
        return None

    # ── 2. gallery-dl ile resim indir ────────────────────────────────────
    logger.info("X video bulunamadı, resim olarak deneniyor: %s", url)
    session = _session_dir()
    try:
        _run_gallery_dl(url, session)
    except Exception as exc:
        logger.error("X gallery-dl hatası: %s", exc)
        cleanup(session)
        return None

    image_files = _collect_files(session, (".jpg", ".jpeg", ".png", ".webp"))
    if image_files:
        selected = [f for f in image_files[:MAX_IMAGES] if _check_size(f)]
        if selected:
            return MediaResult(
                media_type="images", files=selected, caption="", work_dir=session
            )

    logger.warning("X resim bulunamadı (gallery-dl): %s", url)
    cleanup(session)
    return None


# ── Ana Fonksiyon ─────────────────────────────────────────────────────────────

def download_media(url: str) -> Optional[MediaResult]:
    """URL'den medya indir (Instagram veya X)."""
    if "instagram.com" in url:
        return _download_instagram(url)
    elif "twitter.com" in url or "x.com" in url:
        return _download_x(url)
    else:
        logger.warning("Bilinmeyen platform: %s", url)
        return None
