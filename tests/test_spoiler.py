import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot import build_media_caption, send_images, send_video
from downloader import MediaResult
from media_preprocessor import (
    analysis_audio_windows,
    frame_count_for_duration,
    representative_timestamps,
)
from spoiler_models import SpoilerAssessment, should_hide_as_spoiler
from spoiler_analyzer import SpoilerDecisionSet


def assessment(**overrides):
    values = {
        "item_index": 0,
        "is_anime": True,
        "anime_title": "Test Anime",
        "anime_confidence": 0.9,
        "contains_story_information": True,
        "spoiler_severity": 3,
        "spoiler_confidence": 0.9,
        "categories": ["dialogue_reveal"],
        "reason": "Important information is revealed.",
    }
    values.update(overrides)
    return SpoilerAssessment(**values)


class SpoilerDecisionTests(unittest.TestCase):
    def decide(self, item, failure_policy="spoiler"):
        return should_hide_as_spoiler(item, 3, 0.65, failure_policy)

    def test_meaningful_anime_story_information_is_hidden(self):
        self.assertTrue(self.decide(assessment(spoiler_severity=3)))

    def test_harmless_anime_content_is_not_hidden(self):
        self.assertFalse(
            self.decide(
                assessment(
                    contains_story_information=False,
                    spoiler_severity=1,
                    categories=["harmless"],
                )
            )
        )

    def test_confident_non_anime_is_not_hidden(self):
        self.assertFalse(
            self.decide(
                assessment(
                    is_anime=False,
                    anime_title=None,
                    anime_confidence=0.95,
                    spoiler_severity=0,
                    categories=["non_anime"],
                )
            )
        )

    def test_uncertain_result_uses_failure_policy(self):
        item = assessment(spoiler_confidence=0.2, spoiler_severity=1)
        self.assertTrue(self.decide(item, "spoiler"))
        self.assertFalse(self.decide(item, "normal"))

    def test_unknown_string_becomes_null_title(self):
        self.assertIsNone(assessment(anime_title="unknown").anime_title)


class SamplingTests(unittest.TestCase):
    def test_dynamic_frame_count_is_capped(self):
        self.assertEqual(frame_count_for_duration(20), 4)
        self.assertEqual(frame_count_for_duration(90), 6)
        self.assertEqual(frame_count_for_duration(240), 8)
        self.assertEqual(frame_count_for_duration(400), 10)
        self.assertEqual(frame_count_for_duration(1000), 12)
        self.assertEqual(frame_count_for_duration(1000, maximum=7), 7)

    def test_timestamps_span_video_without_endpoints(self):
        timestamps = representative_timestamps(100, 4)
        self.assertEqual(len(timestamps), 4)
        self.assertGreater(timestamps[0], 0)
        self.assertLess(timestamps[-1], 100)
        self.assertEqual(timestamps, sorted(timestamps))

    def test_long_audio_uses_start_middle_end(self):
        windows = analysis_audio_windows(3600, 900)
        self.assertEqual(len(windows), 3)
        self.assertEqual(windows[0][0], 0)
        self.assertAlmostEqual(windows[-1][0] + windows[-1][1], 3600)


class TelegramSpoilerTests(unittest.IsolatedAsyncioTestCase):
    async def test_video_flag_is_forwarded_to_telegram(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "video.mp4"
            path.write_bytes(b"test")
            message = SimpleNamespace(message_id=42, reply_video=AsyncMock())
            update = SimpleNamespace(effective_message=message)
            result = MediaResult(media_type="video", files=[str(path)])

            await send_video(update, result, has_spoiler=True)

            self.assertTrue(message.reply_video.await_args.kwargs["has_spoiler"])

    async def test_each_album_image_gets_its_own_flag(self):
        with TemporaryDirectory() as directory:
            paths = []
            for index in range(2):
                path = Path(directory) / f"image-{index}.jpg"
                path.write_bytes(b"test")
                paths.append(str(path))
            message = SimpleNamespace(message_id=42, reply_media_group=AsyncMock())
            update = SimpleNamespace(effective_message=message)
            result = MediaResult(media_type="images", files=paths)

            await send_images(update, result, [True, False])

            media = message.reply_media_group.await_args.kwargs["media"]
            self.assertTrue(media[0].has_spoiler)
            self.assertFalse(media[1].has_spoiler)


class SpoilerCaptionTests(unittest.TestCase):
    def test_known_anime_is_added_before_original_caption(self):
        result = MediaResult(media_type="video", files=["video.mp4"], caption="Post")
        decision = SpoilerDecisionSet(
            flags=[True], analyzed=True, assessments=[assessment(anime_title="Frieren")]
        )

        self.assertEqual(
            build_media_caption(result, decision),
            "⚠️ Spoiler — Frieren\n\nPost",
        )

    def test_non_spoiler_caption_is_unchanged(self):
        result = MediaResult(media_type="video", files=["video.mp4"], caption="Post")
        decision = SpoilerDecisionSet(
            flags=[False], analyzed=True, assessments=[assessment()]
        )

        self.assertEqual(build_media_caption(result, decision), "Post")

    def test_unknown_anime_is_named_explicitly(self):
        result = MediaResult(media_type="images", files=["image.jpg"])
        decision = SpoilerDecisionSet(flags=[True], analyzed=False, partial=True)

        self.assertEqual(
            build_media_caption(result, decision), "⚠️ Spoiler — Bilinmeyen anime"
        )

    def test_album_lists_unique_spoiler_anime_names(self):
        result = MediaResult(media_type="images", files=["1.jpg", "2.jpg", "3.jpg"])
        decision = SpoilerDecisionSet(
            flags=[True, True, False],
            analyzed=True,
            assessments=[
                assessment(item_index=0, anime_title="One Piece"),
                assessment(item_index=1, anime_title="one piece"),
                assessment(item_index=2, anime_title="Bleach"),
            ],
        )

        self.assertEqual(build_media_caption(result, decision), "⚠️ Spoiler — One Piece")


if __name__ == "__main__":
    unittest.main()
