from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .providers import OpenAICompatibleClient


@dataclass(slots=True)
class TranslationRequest:
    source_language: str
    regions: list[dict[str, Any]]
    character_context: list[dict[str, Any]]
    forced_terms: list[dict[str, Any]]
    translation_memory: list[dict[str, Any]]
    chapter_summary: str = ""


@dataclass(slots=True)
class TranslationResponse:
    translations: list[dict[str, Any]]
    chapter_notes: str = ""


class TranslationAdapter:
    """Translate source text directly into Simplified Chinese."""

    def __init__(
        self,
        client: OpenAICompatibleClient,
        *,
        provider_name: str,
        model_name: str,
    ):
        self.client = client
        self.provider_name = provider_name
        self.model_name = model_name

    async def translate(self, request: TranslationRequest) -> TranslationResponse:
        compact_regions = [
            {
                "region_id": index,
                "source_text": region.get("text", ""),
                "speaker_id": region.get("speaker_id"),
                "speaker_name": region.get("speaker_name"),
                "speech_type": region.get("speech_type", "unknown"),
                "context": region.get("visual_evidence", ""),
            }
            for index, region in enumerate(request.regions)
            if str(region.get("text") or "").strip()
        ]
        prompt = (
            "Translate manga dialogue directly from the source language into "
            "Simplified Chinese using natural Taiwanese Mandarin wording. "
            "Do not route through English. Preserve speaker voice, honorifics, "
            "names, and speech type. Confirmed terms are mandatory; use "
            "translation memory only when context matches. Return JSON only: "
            "{\"translations\":[{\"region_id\":0,\"translation\":\"...\","
            "\"confidence\":0.0,\"notes\":\"\"}],"
            "\"chapter_notes\":\"\"}. "
            f"Source language: {request.source_language}. "
            f"Chapter summary: {request.chapter_summary}. "
            f"Characters: {request.character_context}. "
            f"Forced terms: {request.forced_terms}. "
            f"Translation memory: {request.translation_memory}. "
            f"Regions: {compact_regions}"
        )
        result = await self.client.chat_json(
            [
                {
                    "role": "system",
                    "content": (
                        "You are a meticulous manga localization editor. "
                        "Never invent facts. Output valid JSON without markdown."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_tokens=min(
                8192,
                max(512, 256 + len(compact_regions) * 120),
            ),
        )
        translations = result.get("translations")
        if not isinstance(translations, list):
            translations = []
        return TranslationResponse(
            translations=translations,
            chapter_notes=str(result.get("chapter_notes") or ""),
        )
