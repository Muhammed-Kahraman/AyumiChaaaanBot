import random
import unittest

import quiz


class QuizBankTests(unittest.TestCase):
    def test_bank_has_at_least_one_thousand_questions(self):
        self.assertGreaterEqual(len(quiz.QUESTIONS), 1000)

    def test_questions_have_four_valid_options(self):
        for question in quiz.QUESTIONS:
            self.assertEqual(len(question.options), 4)
            self.assertGreaterEqual(question.correct_option_id, 0)
            self.assertLess(question.correct_option_id, 4)

    def test_random_question_is_reproducible_with_rng(self):
        first = quiz.random_question(random.Random(7))
        second = quiz.random_question(random.Random(7))
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
