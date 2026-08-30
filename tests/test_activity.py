import tempfile
import time
import unittest
from types import SimpleNamespace

import activity


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.old_db = activity.ACTIVITY_DB_FILE
        self.old_owner = activity.ACTIVITY_OWNER_USER_ID
        activity.ACTIVITY_DB_FILE = self.directory.name + "/activity.sqlite3"
        activity.ACTIVITY_OWNER_USER_ID = 99

    def tearDown(self):
        activity.ACTIVITY_DB_FILE = self.old_db
        activity.ACTIVITY_OWNER_USER_ID = self.old_owner
        self.directory.cleanup()

    def user(self, user_id, username):
        return SimpleNamespace(
            id=user_id, username=username, first_name=username, last_name=""
        )

    def test_successful_link_is_awarded_once(self):
        user = self.user(1, "alice")
        self.assertTrue(activity.record_link(-100, "supergroup", user, 10, "https://x/1"))
        self.assertFalse(activity.record_link(-100, "supergroup", user, 10, "https://x/1"))
        self.assertEqual(activity.member_summary(-100, 1)["total_points"], 2)

    def test_three_votes_award_bonus_and_owner_does_not_score(self):
        users = [self.user(1, "alice"), self.user(2, "bob"), self.user(3, "carol")]
        for user in users:
            self.assertTrue(activity.record_spoiler_vote(-100, "supergroup", user, "vote"))
        activity.record_resolution_bonus(-100, "supergroup", "vote", 3)
        self.assertEqual(activity.member_summary(-100, 1)["total_points"], 1)
        self.assertEqual(activity.member_summary(-100, 3)["total_points"], 3)
        self.assertIsNone(activity.member_summary(-100, 99))

    def test_only_owner_can_apply_penalty(self):
        user = self.user(1, "alice")
        activity.record_spoiler_vote(-100, "supergroup", user, "vote")
        review_id = activity.create_penalty_reviews(-100, "vote", {1})[0]
        self.assertFalse(activity.apply_penalty(review_id, 2))
        self.assertTrue(activity.apply_penalty(review_id, 99))
        self.assertEqual(activity.member_summary(-100, 1)["total_points"], 0)

    def test_correct_quiz_answer_awards_once_and_wrong_answer_is_free(self):
        activity.register_quiz("poll-1", -100, 77, 2, time.time() + 120)
        self.assertFalse(
            activity.record_quiz_answer("poll-1", self.user(1, "alice"), [1])
        )
        self.assertIsNone(activity.member_summary(-100, 1))

        activity.register_quiz("poll-2", -100, 78, 2, time.time() + 120)
        self.assertTrue(
            activity.record_quiz_answer("poll-2", self.user(1, "alice"), [2])
        )
        self.assertFalse(
            activity.record_quiz_answer("poll-2", self.user(1, "alice"), [2])
        )
        self.assertEqual(activity.member_summary(-100, 1)["total_points"], 3)

    def test_duplicate_recommendation_is_not_reported_as_limit(self):
        user = self.user(1, "alice")
        self.assertEqual(
            activity.recommendation_status(-100, "supergroup", user, 10, "Anime"),
            "recorded",
        )
        self.assertEqual(
            activity.recommendation_status(-100, "supergroup", user, 10, "Anime"),
            "duplicate",
        )
        self.assertEqual(
            activity.recommendation_status(-100, "supergroup", user, 11, "Anime 2"),
            "recorded",
        )
        self.assertEqual(
            activity.recommendation_status(-100, "supergroup", user, 12, "Anime 3"),
            "recorded",
        )
        self.assertEqual(
            activity.recommendation_status(-100, "supergroup", user, 13, "Anime 4"),
            "limit",
        )


if __name__ == "__main__":
    unittest.main()
