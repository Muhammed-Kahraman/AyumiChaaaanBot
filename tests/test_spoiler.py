import unittest
import os
import tempfile
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot import (
    _leaderboard_text,
    _recent_urls,
    _caption_for_spoiler_state,
    _limit_description,
    _spoiler_caption_entities,
    _spoiler_vote_report,
    _spoiler_votes,
    build_media_caption,
    handle_penalty_review,
    handle_spoiler_vote,
    seen_recently,
    send_images,
    send_video,
    SpoilerVote,
)
from downloader import MediaResult
from media_preprocessor import (
    analysis_audio_windows,
    frame_count_for_duration,
    representative_timestamps,
)
from spoiler_models import SpoilerAssessment, should_hide_as_spoiler
from spoiler_analyzer import SpoilerDecisionSet
from cost_tracker import BudgetExceeded, CostTracker, UsageCost


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

    def test_story_information_is_hidden_when_anime_is_unrecognized(self):
        self.assertTrue(
            self.decide(
                assessment(
                    is_anime=False,
                    anime_title=None,
                    anime_confidence=0.95,
                    contains_story_information=True,
                    spoiler_severity=4,
                )
            )
        )

    def test_new_transformation_is_hidden_at_minor_model_severity(self):
        self.assertTrue(
            self.decide(
                assessment(
                    categories=["transformation"],
                    contains_story_information=False,
                    spoiler_severity=2,
                )
            )
        )

    def test_uncertain_result_uses_failure_policy(self):
        item = assessment(spoiler_confidence=0.2, spoiler_severity=1)
        self.assertTrue(self.decide(item, "spoiler"))
        self.assertFalse(self.decide(item, "normal"))

    def test_unknown_string_becomes_null_title(self):
        self.assertIsNone(assessment(anime_title="unknown").anime_title)


class CostTrackerTests(unittest.TestCase):
    def test_records_usage_and_blocks_at_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "usage.json")
            tracker = CostTracker(path, 0.000001)
            tracker.record("gpt-5.6-luna", UsageCost(5, 1), "vision")
            with self.assertRaises(BudgetExceeded):
                tracker.ensure_available()


class RecentUrlTests(unittest.TestCase):
    def setUp(self):
        _recent_urls.clear()

    def test_same_url_is_deduplicated_inside_one_chat(self):
        url = "https://www.instagram.com/reel/example/?first=1"
        self.assertFalse(seen_recently(url, chat_id=100))
        self.assertTrue(seen_recently(url, chat_id=100))

    def test_same_url_is_independent_between_private_and_group_chats(self):
        url = "https://www.instagram.com/reel/example/?first=1"
        self.assertFalse(seen_recently(url, chat_id=100))
        self.assertFalse(seen_recently(url, chat_id=-200))


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

            with patch("bot._new_spoiler_vote", return_value="test-token"), patch(
                "bot._bind_spoiler_vote"
            ):
                await send_video(update, result, has_spoiler=True)

            self.assertTrue(message.reply_video.await_args.kwargs["has_spoiler"])
            markup = message.reply_video.await_args.kwargs["reply_markup"]
            self.assertIn("Spoiler değil (0/3)", markup.inline_keyboard[0][0].text)

    async def test_third_vote_edits_existing_message(self):
        token = "edit-test-token"
        initial_tokens = set(_spoiler_votes)
        _spoiler_votes[token] = SpoilerVote(
            chat_id=100,
            source_message_id=19,
            media_type="video",
            file_id="telegram-file-id",
            caption="⚠️ Spoiler — Frieren\n\n" + "a" * 150,
            voters={1, 2},
            created_at=0,
            desired_spoiler=False,
        )
        message = SimpleNamespace(
            chat_id=100,
            message_id=20,
            edit_media=AsyncMock(),
        )
        query = SimpleNamespace(
            data=f"spoiler_vote:{token}",
            from_user=SimpleNamespace(id=3),
            message=message,
            answer=AsyncMock(),
            edit_message_reply_markup=AsyncMock(),
        )
        update = SimpleNamespace(callback_query=query)

        try:
            with patch("bot._save_spoiler_votes"):
                await handle_spoiler_vote(update, SimpleNamespace())
        finally:
            for created_token in set(_spoiler_votes) - initial_tokens:
                _spoiler_votes.pop(created_token, None)
            _spoiler_votes.pop(token, None)

        message.edit_media.assert_awaited_once()
        media = message.edit_media.await_args.kwargs["media"]
        self.assertFalse(media.has_spoiler)
        self.assertEqual(media.caption, "a" * 99 + "…")
        self.assertEqual(
            message.edit_media.await_args.kwargs["reply_markup"].inline_keyboard[0][0].text,
            "⚠️ Spoiler bildir (0/3)",
        )

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

    async def test_penalty_review_closes_only_selected_row_and_last_removes_keyboard(self):
        message = SimpleNamespace(
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✅ -2 uygula", callback_data="activity_penalty:approve:1"
                        ),
                        InlineKeyboardButton(
                            "❌ Ceza verme", callback_data="activity_penalty:reject:1"
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            "✅ -2 uygula", callback_data="activity_penalty:approve:2"
                        ),
                        InlineKeyboardButton(
                            "❌ Ceza verme", callback_data="activity_penalty:reject:2"
                        ),
                    ],
                ]
            )
        )
        query = SimpleNamespace(
            data="activity_penalty:approve:1",
            from_user=SimpleNamespace(id=99),
            message=message,
            answer=AsyncMock(),
            edit_message_reply_markup=AsyncMock(),
        )

        with patch("bot.activity.is_owner", return_value=True), patch(
            "bot.activity.apply_penalty", return_value=True
        ):
            await handle_penalty_review(SimpleNamespace(callback_query=query), SimpleNamespace())

        remaining_markup = query.edit_message_reply_markup.await_args.kwargs["reply_markup"]
        self.assertEqual(len(remaining_markup.inline_keyboard), 1)
        self.assertTrue(
            all(button.callback_data.endswith(":2") for button in remaining_markup.inline_keyboard[0])
        )

        message.reply_markup = remaining_markup
        query.data = "activity_penalty:reject:2"
        query.edit_message_reply_markup.reset_mock()
        with patch("bot.activity.is_owner", return_value=True), patch(
            "bot.activity.reject_penalty", return_value=True
        ):
            await handle_penalty_review(SimpleNamespace(callback_query=query), SimpleNamespace())
        self.assertIsNone(
            query.edit_message_reply_markup.await_args.kwargs["reply_markup"]
        )

    def test_spoiler_report_includes_new_and_legacy_votes_separately(self):
        initial_tokens = set(_spoiler_votes)
        url = "https://x.com/example/status/123?si=abc"
        _spoiler_votes["new-report-token"] = SpoilerVote(
            chat_id=-100,
            source_message_id=12,
            media_type="video",
            file_id="file-1",
            caption="caption",
            voters={1},
            created_at=1,
            desired_spoiler=True,
            source_url=url,
        )
        _spoiler_votes["legacy-report-token"] = SpoilerVote(
            chat_id=-100,
            source_message_id=77,
            media_type="video",
            file_id="file-2",
            caption="caption",
            voters={2},
            created_at=2,
            desired_spoiler=False,
        )
        try:
            with patch("bot.activity.link_message_sources", return_value=[(-100, 77)]), patch(
                "bot.activity.member_label", side_effect=lambda chat_id, user_id: f"@user{user_id}"
            ):
                report = _spoiler_vote_report(url)
        finally:
            for token in set(_spoiler_votes) - initial_tokens:
                _spoiler_votes.pop(token, None)

        self.assertIn("@user1", report)
        self.assertIn("@user2", report)
        self.assertIn("Spoiler diyenler", report)
        self.assertIn("Spoiler değil diyenler", report)
        self.assertIn("Oylama turu: 2", report)

    def test_leaderboard_starts_with_owner_and_title(self):
        with patch("bot.activity.owner_label_html", return_value="@herooflatvia"), patch(
            "bot.activity.leaderboard", return_value=[]
        ):
            report = _leaderboard_text(-100)
        self.assertTrue(report.startswith("👑 Yönetici: @herooflatvia"))
        self.assertIn("Kozmik Hakem-i Mutlak", report)


class SpoilerCaptionTests(unittest.TestCase):
    def test_description_length_limit(self):
        self.assertEqual(_limit_description("a" * 99), "a" * 99)
        self.assertEqual(_limit_description("a" * 100), "a" * 100)
        limited = _limit_description("a" * 101)
        self.assertEqual(len(limited), 100)
        self.assertEqual(limited, "a" * 99 + "…")

    def test_spoiler_caption_hides_original_description(self):
        result = MediaResult(media_type="video", files=["video.mp4"], caption="Post")
        decision = SpoilerDecisionSet(
            flags=[True], analyzed=True, assessments=[assessment(anime_title="Frieren")]
        )

        self.assertEqual(
            build_media_caption(result, decision),
            "⚠️ Spoiler — Frieren\n\nPost",
        )

    def test_spoiler_caption_limits_description_but_keeps_anime_name(self):
        description = "a" * 101
        result = MediaResult(
            media_type="video", files=["video.mp4"], caption=description
        )
        decision = SpoilerDecisionSet(
            flags=[True], analyzed=True, assessments=[assessment(anime_title="Frieren")]
        )
        caption = build_media_caption(result, decision)
        self.assertTrue(caption.startswith("⚠️ Spoiler — Frieren\n\n"))
        self.assertEqual(caption.split("\n\n", 1)[1], "a" * 99 + "…")

    def test_vote_caption_state_also_limits_description(self):
        description = "a" * 150
        caption = _caption_for_spoiler_state(
            f"⚠️ Spoiler — Frieren\n\n{description}", True
        )
        self.assertEqual(caption, "⚠️ Spoiler — Frieren\n\n" + "a" * 99 + "…")

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
            build_media_caption(result, decision), "⚠️ Spoiler"
        )

    def test_album_spoiler_caption_hides_original_description(self):
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

        self.assertEqual(
            build_media_caption(result, decision), "⚠️ Spoiler — One Piece"
        )

    def test_low_confidence_anime_title_is_not_shown(self):
        result = MediaResult(media_type="video", files=["video.mp4"])
        decision = SpoilerDecisionSet(
            flags=[True], analyzed=True,
            assessments=[assessment(anime_title="Wrong Anime", anime_confidence=0.4)],
        )
        self.assertEqual(build_media_caption(result, decision), "⚠️ Spoiler")

    def test_spoiler_caption_entity_hides_only_caption_body(self):
        entities = _spoiler_caption_entities("⚠️ Spoiler — Frieren\n\nPost")
        self.assertEqual(len(entities), 1)
        self.assertGreater(entities[0].offset, 0)
        self.assertGreater(entities[0].length, 0)


if __name__ == "__main__":
    unittest.main()
