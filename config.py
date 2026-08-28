"""AyumuChanBot — Configuration"""

import os
import re

from dotenv import load_dotenv

load_dotenv()


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.getenv(name, str(default))))
    except ValueError:
        return default

# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# ── Instagram ─────────────────────────────────────────────────────────────────
# Instagram sessionid'yi tek başına kabul etmiyor — tarayıcıdaki yardımcı
# cookie'ler (csrftoken, ds_user_id, mid…) olmadan istekler login'e yönleniyor.
IG_SESSIONID = os.getenv("IG_SESSIONID", "")
IG_CSRFTOKEN = os.getenv("IG_CSRFTOKEN", "")
IG_DS_USER_ID = os.getenv("IG_DS_USER_ID", "")
IG_MID = os.getenv("IG_MID", "")
IG_DID = os.getenv("IG_DID", "")
IG_DATR = os.getenv("IG_DATR", "")

IG_COOKIES = {
    "sessionid": IG_SESSIONID,
    "csrftoken": IG_CSRFTOKEN,
    "ds_user_id": IG_DS_USER_ID,
    "mid": IG_MID,
    "ig_did": IG_DID,
    "datr": IG_DATR,
}

# ── X (Twitter) ───────────────────────────────────────────────────────────────
# X guest token erişimini kapattı — resim tweetleri için giriş cookie'si şart.
X_AUTH_TOKEN = os.getenv("X_AUTH_TOKEN", "")
X_CT0 = os.getenv("X_CT0", "")
X_TWID = os.getenv("X_TWID", "")
X_GUEST_ID = os.getenv("X_GUEST_ID", "")

X_COOKIES = {
    "auth_token": X_AUTH_TOKEN,
    "ct0": X_CT0,
    "twid": X_TWID,
    "guest_id": X_GUEST_ID,
}

# ── URL Patterns ──────────────────────────────────────────────────────────────
INSTAGRAM_PATTERN = re.compile(
    r"https?://(?:www\.)?instagram\.com/"
    r"(?:p|reel|reels|tv)/[\w-]+/?(?:\?[^\s]*)?"
)

X_PATTERN = re.compile(
    r"https?://(?:www\.)?(?:twitter\.com|x\.com)"
    r"/\w+/status/\d+(?:/(?:photo|video)/\d+)?(?:\?[^\s]*)?"
)

# ── Download Settings ─────────────────────────────────────────────────────────
MAX_IMAGES = 20                          # Gönderilecek max resim sayısı
TELEGRAM_ALBUM_LIMIT = 10                # Telegram'ın albüm başına medya limiti
MAX_FILE_SIZE = 50 * 1024 * 1024        # 50 MB — Telegram limiti
DOWNLOAD_DIR = "/tmp/ayumu_downloads"   # Geçici indirme dizini

# ── AI Spoiler Detection ────────────────────────────────────────────────────────────────
AI_SPOILER_ENABLED = _env_bool("AI_SPOILER_ENABLED", False)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_TRANSCRIPTION_MODEL = os.getenv(
    "OPENAI_TRANSCRIPTION_MODEL", "gpt-4o-mini-transcribe"
)
OPENAI_VISION_MODEL = os.getenv("OPENAI_VISION_MODEL", "gpt-5.6-luna")
OPENAI_REASONING_EFFORT = os.getenv(
    "OPENAI_REASONING_EFFORT", "none"
).strip().lower()
if OPENAI_REASONING_EFFORT not in {
    "none", "minimal", "low", "medium", "high", "xhigh", "max"
}:
    OPENAI_REASONING_EFFORT = "none"

SPOILER_THRESHOLD = min(5, _env_int("SPOILER_THRESHOLD", 3))
SPOILER_MIN_CONFIDENCE = min(
    1.0, _env_float("SPOILER_MIN_CONFIDENCE", 0.65)
)
AI_FAILURE_POLICY = os.getenv("AI_FAILURE_POLICY", "spoiler").strip().lower()
if AI_FAILURE_POLICY not in {"spoiler", "normal"}:
    AI_FAILURE_POLICY = "spoiler"

AI_MAX_FRAMES = max(1, _env_int("AI_MAX_FRAMES", 12, 1))
AI_FRAME_MAX_DIMENSION = max(
    256, _env_int("AI_FRAME_MAX_DIMENSION", 960, 256)
)
AI_FRAME_JPEG_QUALITY = min(
    31, max(2, _env_int("AI_FRAME_JPEG_QUALITY", 5, 2))
)
AI_MAX_VIDEO_DURATION_SECONDS = max(
    60, _env_int("AI_MAX_VIDEO_DURATION_SECONDS", 900, 60)
)
AI_AUDIO_BITRATE_KBPS = max(24, _env_int("AI_AUDIO_BITRATE_KBPS", 40, 24))
AI_TIMEOUT_SECONDS = max(10, _env_int("AI_TIMEOUT_SECONDS", 90, 10))
AI_MAX_RETRIES = min(5, _env_int("AI_MAX_RETRIES", 2))
AI_JOB_CONCURRENCY = max(1, _env_int("AI_JOB_CONCURRENCY", 2, 1))
MEDIA_QUEUE_CAPACITY = max(1, _env_int("MEDIA_QUEUE_CAPACITY", 50, 1))

FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg")
FFPROBE_PATH = os.getenv("FFPROBE_PATH", "ffprobe")
