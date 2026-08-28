#!/usr/bin/env python3
"""Download a shared link and run the spoiler analyzer before production."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile

from downloader import cleanup, download_media
from spoiler_analyzer import OpenAISpoilerAnalyzer


VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".mkv"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "media",
        help="Test edilecek Instagram/X linki veya yerel video/resim",
    )
    parser.add_argument("--caption", default="", help="Opsiyonel kaynak başlığı")
    args = parser.parse_args()

    analyzer = OpenAISpoilerAnalyzer()
    downloaded = None
    try:
        if args.media.startswith(("https://", "http://")):
            loop = asyncio.get_running_loop()
            downloaded = await loop.run_in_executor(None, download_media, args.media)
            if downloaded is None:
                parser.error("Linkten medya indirilemedi")
            if downloaded.media_type == "video":
                decision = await analyzer.analyze_video(
                    downloaded.files[0],
                    downloaded.work_dir,
                    downloaded.caption or args.caption,
                )
            else:
                decision = await analyzer.analyze_images(
                    downloaded.files,
                    downloaded.work_dir,
                    downloaded.caption or args.caption,
                )
        else:
            path = os.path.abspath(args.media)
            if not os.path.isfile(path):
                parser.error(f"Dosya bulunamadı: {path}")
            extension = os.path.splitext(path)[1].lower()
            if extension not in VIDEO_EXTENSIONS | IMAGE_EXTENSIONS:
                parser.error(f"Desteklenmeyen dosya türü: {extension}")
            with tempfile.TemporaryDirectory(
                prefix="ayumu-spoiler-check-"
            ) as work_dir:
                if extension in VIDEO_EXTENSIONS:
                    decision = await analyzer.analyze_video(
                        path, work_dir, args.caption
                    )
                else:
                    decision = await analyzer.analyze_images(
                        [path], work_dir, args.caption
                    )
    finally:
        if downloaded and downloaded.work_dir:
            cleanup(downloaded.work_dir)

    print(
        json.dumps(
            {
                "has_spoiler": decision.flags,
                "analyzed": decision.analyzed,
                "partial": decision.partial,
                "assessments": [
                    item.model_dump() for item in (decision.assessments or [])
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
