"""Typed models and deterministic spoiler decision policy."""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


SpoilerCategory = Literal[
    "death",
    "major_injury",
    "identity_reveal",
    "relationship_reveal",
    "betrayal",
    "plot_twist",
    "backstory",
    "dialogue_reveal",
    "battle_outcome",
    "transformation",
    "alliance_reveal",
    "mystery_resolution",
    "future_event",
    "minor_story_info",
    "harmless",
    "non_anime",
    "other",
]


class SpoilerAssessment(BaseModel):
    """AI assessment for one video or one image in an album."""

    model_config = ConfigDict(extra="forbid")

    item_index: int = Field(ge=0)
    is_anime: bool
    anime_title: Optional[str] = None
    anime_confidence: float = Field(ge=0.0, le=1.0)
    contains_story_information: bool
    spoiler_severity: int = Field(ge=0, le=5)
    spoiler_confidence: float = Field(ge=0.0, le=1.0)
    categories: List[SpoilerCategory] = Field(default_factory=list, max_length=5)
    reason: str = Field(max_length=240)

    @model_validator(mode="after")
    def normalize_unknown_title(self) -> "SpoilerAssessment":
        if self.anime_title and self.anime_title.strip().lower() == "unknown":
            self.anime_title = None
        return self


class SpoilerAnalysisResponse(BaseModel):
    """Structured result returned by the vision model."""

    model_config = ConfigDict(extra="forbid")

    items: List[SpoilerAssessment] = Field(min_length=1)


def fallback_has_spoiler(failure_policy: str) -> bool:
    return failure_policy == "spoiler"


def should_hide_as_spoiler(
    assessment: SpoilerAssessment,
    threshold: int,
    min_confidence: float,
    failure_policy: str,
) -> bool:
    """Make the Telegram decision locally instead of trusting an AI boolean."""
    fallback = fallback_has_spoiler(failure_policy)

    if assessment.spoiler_confidence < min_confidence:
        return fallback

    if not assessment.is_anime:
        if assessment.anime_confidence >= min_confidence:
            return False
        return fallback

    return assessment.spoiler_severity >= threshold
