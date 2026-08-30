"""Spoiler-free anime quiz question bank."""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class QuizQuestion:
    question: str
    options: tuple[str, ...]
    correct_option_id: int


def _load_questions() -> tuple[QuizQuestion, ...]:
    path = Path(__file__).with_name("quiz_questions.json")
    with path.open(encoding="utf-8") as question_file:
        raw_questions = json.load(question_file)
    questions = tuple(
        QuizQuestion(
            question=str(item["question"]),
            options=tuple(str(option) for option in item["options"]),
            correct_option_id=int(item["correct_option_id"]),
        )
        for item in raw_questions
        if len(item.get("options", [])) == 4
    )
    if len(questions) < 1000:
        raise RuntimeError(
            f"Quiz havuzu en az 1000 soru içermeli, bulunan: {len(questions)}"
        )
    return questions


QUESTIONS = _load_questions()


def random_question(rng: Optional[random.Random] = None) -> QuizQuestion:
    return (rng or random).choice(QUESTIONS)
