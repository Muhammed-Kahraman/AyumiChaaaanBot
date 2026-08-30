#!/usr/bin/env python3
"""Build a large spoiler-free anime quiz bank from AniList metadata."""

from __future__ import annotations

import json
import random
import sys
import urllib.request
from pathlib import Path


API_URL = "https://graphql.anilist.co"
TARGET_QUESTIONS = 1000
PAGE_SIZE = 50
OUTPUT = Path(__file__).resolve().parents[1] / "quiz_questions.json"

QUERY = """
query ($page: Int!, $perPage: Int!) {
  Page(page: $page, perPage: $perPage) {
    media(type: ANIME, sort: POPULARITY_DESC) {
      id
      title { romaji english }
      genres
      format
      source
      episodes
      studios(isMain: true) { nodes { name } }
    }
  }
}
"""

FORMAT_LABELS = {
    "TV": "TV serisi", "TV_SHORT": "kısa TV serisi", "MOVIE": "film",
    "SPECIAL": "özel bölüm", "OVA": "OVA", "ONA": "ONA", "MUSIC": "müzik videosu",
}
SOURCE_LABELS = {
    "ORIGINAL": "orijinal yapım", "MANGA": "manga", "LIGHT_NOVEL": "light novel",
    "VISUAL_NOVEL": "visual novel", "VIDEO_GAME": "video oyunu", "NOVEL": "roman",
    "WEB_NOVEL": "web romanı", "OTHER": "diğer",
}


def fetch_page(page: int) -> list[dict]:
    payload = json.dumps({"query": QUERY, "variables": {"page": page, "perPage": PAGE_SIZE}}).encode()
    request = urllib.request.Request(
        API_URL,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "AyumuChanBot-QuizBuilder/1.0"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        body = json.load(response)
    if body.get("errors"):
        raise RuntimeError(body["errors"])
    return body["data"]["Page"]["media"]


def title_of(media: dict) -> str:
    title = media.get("title") or {}
    return title.get("english") or title.get("romaji") or "Bilinmeyen anime"


def choices(correct: str, values: list[str], rng: random.Random) -> tuple[list[str], int]:
    pool = [value for value in dict.fromkeys(values) if value and value != correct]
    if len(pool) < 3:
        return [], -1
    options = rng.sample(pool, 3) + [correct]
    rng.shuffle(options)
    return options, options.index(correct)


def make_question(prompt: str, correct: str, values: list[str], rng: random.Random) -> dict | None:
    options, correct_id = choices(correct, values, rng)
    if correct_id < 0:
        return None
    return {"question": prompt, "options": options, "correct_option_id": correct_id}


def build(media_items: list[dict]) -> list[dict]:
    rng = random.Random(20260828)
    usable = [item for item in media_items if title_of(item) != "Bilinmeyen anime"]
    genres = sorted({genre for item in usable for genre in item.get("genres", [])})
    formats = list(FORMAT_LABELS.values())
    sources = list(SOURCE_LABELS.values())
    studios = sorted({
        node["name"]
        for item in usable
        for node in ((item.get("studios") or {}).get("nodes") or [])
        if node.get("name")
    })
    episode_values = sorted({str(item["episodes"]) for item in usable if item.get("episodes")})

    questions: list[dict] = []
    seen: set[str] = set()
    for item in usable:
        title = title_of(item)
        item_genres = item.get("genres") or []
        main_studio = ((item.get("studios") or {}).get("nodes") or [{}])[0].get("name")
        templates: list[dict | None] = []
        if item_genres:
            genre = item_genres[0]
            templates.append(make_question(f"{title} hangi türle ilişkilidir?", genre, genres, rng))
        if item.get("format") in FORMAT_LABELS:
            fmt = FORMAT_LABELS[item["format"]]
            templates.append(make_question(f"{title} hangi formatta yayımlanmıştır?", fmt, formats, rng))
        if item.get("source") in SOURCE_LABELS:
            source = SOURCE_LABELS[item["source"]]
            templates.append(make_question(f"{title} hangi kaynaktan uyarlanmıştır?", source, sources, rng))
        if item.get("episodes"):
            episodes = str(item["episodes"])
            templates.append(make_question(f"{title} kaç bölümden oluşur?", episodes, episode_values, rng))
        if main_studio:
            templates.append(make_question(f"{title} hangi ana stüdyo tarafından hazırlanmıştır?", main_studio, studios, rng))
        for question in templates:
            if question and question["question"] not in seen:
                seen.add(question["question"])
                questions.append(question)
    rng.shuffle(questions)
    return questions[:TARGET_QUESTIONS]


def main() -> int:
    media: list[dict] = []
    for page in range(1, 9):
        print(f"AniList sayfa {page}/8 çekiliyor...", file=sys.stderr)
        media.extend(fetch_page(page))
    questions = build(media)
    if len(questions) < TARGET_QUESTIONS:
        raise RuntimeError(f"Yalnızca {len(questions)} soru üretildi; {TARGET_QUESTIONS} gerekli.")
    OUTPUT.write_text(json.dumps(questions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{len(questions)} soru yazıldı: {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
