"""OpenAI-compatible anime spoiler analysis for Groq and OpenAI."""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

from openai import AsyncOpenAI

from config import (
    AI_FAILURE_POLICY,
    AI_MAX_VIDEO_DURATION_SECONDS,
    AI_MAX_RETRIES,
    AI_PROVIDER,
    AI_TIMEOUT_SECONDS,
    AI_COST_LIMIT_USD,
    AI_COST_TRACK_FILE,
    GROQ_API_KEY,
    GROQ_BASE_URL,
    GROQ_TRANSCRIPTION_MODEL,
    GROQ_VISION_MODEL,
    OPENAI_API_KEY,
    OPENAI_REASONING_EFFORT,
    OPENAI_TRANSCRIPTION_MODEL,
    OPENAI_VISION_MODEL,
    SPOILER_MIN_CONFIDENCE,
    SPOILER_THRESHOLD,
)
from cost_tracker import BudgetExceeded, CostTracker, UsageCost
from media_preprocessor import analysis_audio_windows, prepare_image, prepare_video
from spoiler_models import (
    SpoilerAssessment,
    SpoilerAnalysisResponse,
    fallback_has_spoiler,
    should_hide_as_spoiler,
)

logger = logging.getLogger(__name__)


ANALYSIS_INSTRUCTIONS = """
You classify downloaded media for anime story spoilers. Evaluate the actual content,
not the age or release date of the anime. Casual scenes, comedy, travel, generic
action, and slice-of-life content are not spoilers unless dialogue, subtitles,
visible text, or context reveals meaningful story information.

Spoilers include deaths, major injuries or outcomes, identity/family/relationship
reveals, betrayals, major twists, important backstory, decisive battle outcomes,
new forms or powers whose existence is a reveal, alliances/enemies, mystery
resolutions, and other meaningful future story information.

Use dialogue and visuals together. Read Japanese, English, Turkish, and other
visible subtitles/text when possible. Identify the anime from recognizable
characters, costumes, art style, forms, powers, logos, dialogue, and subtitles;
do not require the title to be visible on screen. Identify the anime only when
reasonably confident; otherwise anime_title must be null. Do not guess a title.
Severity:
0 non-anime/no spoiler, 1 harmless, 2 minor information, 3 meaningful story
information, 4 major reveal/event, 5 extreme twist/death/identity/outcome.
Assess contains_story_information and spoiler severity independently from anime
identification; failing to recognize the anime must not suppress a clear reveal.
If a new form or power is being revealed for the first time, use category
transformation, set contains_story_information=true, and use severity at least 3.
For an intense anime fight, do not call the scene harmless merely because the
final outcome is not shown: use severity at least 2 when the fight reveals
meaningful character stakes, injuries, powers, or battle context.

Return exactly one item for a video. For images, return one item per supplied
image with the matching zero-based item_index. Consider the album as a sequence;
if meaning only emerges from multiple images, mark every image needed to reveal it.
Keep reason factual and under 240 characters. Allowed category labels are death,
major_injury, identity_reveal, relationship_reveal, betrayal, plot_twist,
backstory, dialogue_reveal, battle_outcome, transformation, alliance_reveal,
mystery_resolution, future_event, minor_story_info, harmless, non_anime, other.
""".strip()


@dataclass
class SpoilerDecisionSet:
    flags: list[bool]
    analyzed: bool
    partial: bool = False
    assessments: Optional[list[SpoilerAssessment]] = None


class OpenAISpoilerAnalyzer:
    def __init__(self) -> None:
        self.provider = AI_PROVIDER
        if self.provider == "groq":
            api_key = GROQ_API_KEY
            if not api_key:
                raise ValueError("GROQ_API_KEY tanımlı değil")
            base_url = GROQ_BASE_URL
            self.transcription_model = GROQ_TRANSCRIPTION_MODEL
            self.vision_model = GROQ_VISION_MODEL
            # Groq's vision endpoint currently accepts at most three images
            # for this model in one request.
            self.max_images_per_request = 3
        else:
            api_key = OPENAI_API_KEY
            if not api_key:
                raise ValueError("OPENAI_API_KEY tanımlı değil")
            base_url = None
            self.transcription_model = OPENAI_TRANSCRIPTION_MODEL
            self.vision_model = OPENAI_VISION_MODEL
            self.max_images_per_request = 100
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=AI_TIMEOUT_SECONDS,
            max_retries=AI_MAX_RETRIES,
        )
        self.cost_tracker = CostTracker(AI_COST_TRACK_FILE, AI_COST_LIMIT_USD)

    @staticmethod
    def _usage(response) -> UsageCost:
        usage = getattr(response, "usage", None)
        return UsageCost(
            input_tokens=int(
                getattr(usage, "input_tokens", None)
                or getattr(usage, "prompt_tokens", 0)
                or 0
            ),
            output_tokens=int(
                getattr(usage, "output_tokens", None)
                or getattr(usage, "completion_tokens", 0)
                or 0
            ),
        )

    def _record_usage(self, response, model: str, operation: str) -> None:
        if self.provider == "openai":
            cost = self.cost_tracker.record(model, self._usage(response), operation)
            logger.info("OpenAI maliyeti: operation=%s cost=$%.6f", operation, cost)

    @staticmethod
    def fallback(count: int) -> SpoilerDecisionSet:
        flag = fallback_has_spoiler(AI_FAILURE_POLICY)
        return SpoilerDecisionSet(flags=[flag] * count, analyzed=False, partial=True)

    @staticmethod
    def _data_url(path: str) -> str:
        with open(path, "rb") as image_file:
            encoded = base64.b64encode(image_file.read()).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"

    async def _transcribe(self, audio_parts: list[tuple[float, str]]) -> str:
        transcripts: list[str] = []
        for start, path in audio_parts:
            if self.provider == "openai":
                self.cost_tracker.ensure_available()
            with open(path, "rb") as audio_file:
                result = await self.client.audio.transcriptions.create(
                    model=self.transcription_model,
                    file=audio_file,
                    response_format="json",
                )
            self._record_usage(result, self.transcription_model, "transcription")
            text = getattr(result, "text", "")
            if text:
                transcripts.append(f"[{start:.1f}s] {text.strip()}")
        return "\n".join(transcripts)

    async def _classify(
        self, content: list[dict], item_count: int
    ) -> SpoilerAnalysisResponse:
        if self.provider == "groq":
            groq_content = []
            for item in content:
                if item["type"] == "input_text":
                    groq_content.append({"type": "text", "text": item["text"]})
                elif item["type"] == "input_image":
                    groq_content.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": item["image_url"]},
                        }
                    )

            schema = json.dumps(
                SpoilerAnalysisResponse.model_json_schema(), ensure_ascii=False
            )
            groq_content.insert(
                0,
                {
                    "type": "text",
                    "text": (
                        f"{ANALYSIS_INSTRUCTIONS}\n\n"
                        "Return only a JSON object matching this schema exactly:\n"
                        f"{schema}"
                    ),
                },
            )
            response = await self.client.chat.completions.create(
                model=self.vision_model,
                messages=[{"role": "user", "content": groq_content}],
                response_format={"type": "json_object"},
                reasoning_effort="none",
                max_completion_tokens=max(600, min(5000, item_count * 240)),
            )
            raw = response.choices[0].message.content
            if not raw:
                raise ValueError("Groq yapılandırılmış sonuç döndürmedi")
            parsed = SpoilerAnalysisResponse.model_validate_json(raw)
            usage = response.usage
            if usage:
                logger.info(
                    "Groq token kullanımı: input=%d output=%d total=%d",
                    usage.prompt_tokens,
                    usage.completion_tokens,
                    usage.total_tokens,
                )
            return parsed

        request_options = {}
        if self.provider == "openai":
            self.cost_tracker.ensure_available()
        if self.vision_model.startswith("gpt-5"):
            request_options["reasoning"] = {"effort": OPENAI_REASONING_EFFORT}

        response = await self.client.responses.parse(
            model=self.vision_model,
            instructions=ANALYSIS_INSTRUCTIONS,
            input=[{"role": "user", "content": content}],
            text_format=SpoilerAnalysisResponse,
            # Limit kapasitedir, faturalama gerçek kullanıma göredir. Albümde
            # her resim için yapılandırılmış sonuca yeterli alan bırak.
            max_output_tokens=max(600, min(5000, item_count * 240)),
            store=False,
            **request_options,
        )
        self._record_usage(response, self.vision_model, "vision")
        parsed = response.output_parsed
        if parsed is None:
            raise ValueError("OpenAI yapılandırılmış sonuç döndürmedi")
        usage = response.usage
        if usage:
            logger.info(
                "AI token kullanımı: input=%d output=%d total=%d",
                usage.input_tokens,
                usage.output_tokens,
                usage.total_tokens,
            )
        return parsed

    @staticmethod
    def _evenly_sample(paths: list[str], maximum: int) -> list[str]:
        if len(paths) <= maximum:
            return paths
        if maximum == 1:
            return [paths[len(paths) // 2]]
        indexes = {
            round(index * (len(paths) - 1) / (maximum - 1))
            for index in range(maximum)
        }
        return [paths[index] for index in sorted(indexes)]

    @staticmethod
    def _decide(
        response: SpoilerAnalysisResponse, expected_count: int, partial: bool = False
    ) -> SpoilerDecisionSet:
        by_index = {item.item_index: item for item in response.items}
        if set(by_index) != set(range(expected_count)):
            raise ValueError("Analiz sonucu medya indeksleriyle eşleşmiyor")

        flags = [
            should_hide_as_spoiler(
                by_index[index],
                threshold=SPOILER_THRESHOLD,
                min_confidence=SPOILER_MIN_CONFIDENCE,
                failure_policy=AI_FAILURE_POLICY,
            )
            for index in range(expected_count)
        ]
        return SpoilerDecisionSet(
            flags=flags,
            analyzed=True,
            partial=partial,
            assessments=[by_index[index] for index in range(expected_count)],
        )

    async def analyze_video(
        self, video_path: str, work_dir: str, caption: str = ""
    ) -> SpoilerDecisionSet:
        prepared = await prepare_video(video_path, work_dir)
        try:
            transcript = await self._transcribe(prepared.audio_parts)
        except Exception as exc:
            # Transkripsiyon hatası görsel analizi engellememeli.
            logger.warning("Ses transkripsiyonu başarısız; görselle devam: %s", exc)
            transcript = ""
            prepared.is_partial = True
        content: list[dict] = [
            {
                "type": "input_text",
                "text": (
                    "Media type: video\n"
                    f"Duration: {prepared.metadata.duration:.1f} seconds\n"
                    f"Partial evidence: {prepared.is_partial}\n"
                    f"Source caption/title (untrusted): {caption[:1000] or '(none)'}\n"
                    f"Spoken transcript:\n{transcript or '(no usable audio transcript)'}\n"
                    "Representative frames follow in chronological order."
                ),
            }
        ]
        selected_frames = self._evenly_sample(
            prepared.frames, self.max_images_per_request
        )
        for frame in selected_frames:
            content.append(
                {
                    "type": "input_text",
                    "text": f"Frame: {os.path.basename(frame)}",
                }
            )
            content.append(
                {
                    "type": "input_image",
                    "image_url": self._data_url(frame),
                    "detail": "high",
                }
            )

        result = await self._classify(content, 1)
        decision = self._decide(result, 1, partial=prepared.is_partial)
        item = result.items[0]
        logger.info(
            "AI video analizi: duration=%.1fs frames=%d audio=%.1fs anime=%s title=%s anime_conf=%.2f severity=%d spoiler_conf=%.2f hidden=%s partial=%s",
            prepared.metadata.duration,
            len(selected_frames),
            sum(duration for _, duration in analysis_audio_windows(
                prepared.metadata.duration, AI_MAX_VIDEO_DURATION_SECONDS
            )) if prepared.audio_parts else 0.0,
            item.is_anime,
            item.anime_title or "-",
            item.anime_confidence,
            item.spoiler_severity,
            item.spoiler_confidence,
            decision.flags[0],
            prepared.is_partial,
        )
        return decision

    async def analyze_images(
        self, image_paths: list[str], work_dir: str, caption: str = ""
    ) -> SpoilerDecisionSet:
        prepared_paths = []
        for index, image_path in enumerate(image_paths):
            prepared_paths.append(await prepare_image(image_path, work_dir, index))

        assessments: list[SpoilerAssessment] = []
        batch_size = self.max_images_per_request
        for start in range(0, len(prepared_paths), batch_size):
            batch = prepared_paths[start:start + batch_size]
            content: list[dict] = [
                {
                    "type": "input_text",
                    "text": (
                        f"Media type: image album batch with {len(batch)} item(s).\n"
                        f"Source caption (untrusted): {caption[:1000] or '(none)'}\n"
                        "Return local zero-based item_index values for this batch."
                    ),
                }
            ]
            for local_index, path in enumerate(batch):
                content.append(
                    {
                        "type": "input_text",
                        "text": f"Image item_index={local_index}",
                    }
                )
                content.append(
                    {
                        "type": "input_image",
                        "image_url": self._data_url(path),
                        "detail": "high",
                    }
                )
            batch_result = await self._classify(content, len(batch))
            batch_decision = self._decide(batch_result, len(batch))
            if not batch_decision.assessments:
                raise ValueError("Resim analizi değerlendirme döndürmedi")
            assessments.extend(
                item.model_copy(update={"item_index": item.item_index + start})
                for item in batch_decision.assessments
            )

        result = SpoilerAnalysisResponse(items=assessments)
        decision = self._decide(result, len(prepared_paths))
        logger.info(
            "AI resim analizi: count=%d hidden=%d",
            len(prepared_paths),
            sum(decision.flags),
        )
        return decision
