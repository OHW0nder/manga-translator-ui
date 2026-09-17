from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .providers import OpenAICompatibleClient
from .storage import PipelineStorage


@dataclass(slots=True)
class KnowledgeService:
    storage: PipelineStorage
    client: OpenAICompatibleClient

    def build_translation_context(
        self,
        *,
        series_id: str,
        source_language: str,
        regions: list[dict[str, Any]],
        forced_terms: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        character_ids = {
            str(region.get("speaker_id"))
            for region in regions
            if region.get("speaker_id")
        }
        characters = self.storage.list_characters(series_id)
        character_context = [
            character
            for character in characters
            if character["id"] in character_ids
            or character.get("review_status") == "confirmed"
        ]
        memory = []
        seen: set[tuple[str, str]] = set()
        for region in regions:
            source = str(region.get("text") or "").strip()
            if not source:
                continue
            key = (source, str(region.get("speaker_id") or ""))
            if key in seen:
                continue
            seen.add(key)
            memory.extend(
                self.storage.search_translation_memory(
                    series_id=series_id,
                    source_language=source_language,
                    query=source,
                    limit=2,
                )
            )
        return {
            "characters": character_context,
            "forced_terms": (
                forced_terms
                if forced_terms is not None
                else self.storage.list_forced_terms(series_id)
            ),
            "translation_memory": memory,
        }

    async def extract_chapter_knowledge(
        self,
        *,
        chapter_name: str,
        source_language: str,
        pages: list[dict[str, Any]],
        known_characters: list[dict[str, Any]],
    ) -> dict[str, Any]:
        payload = []
        for page in pages:
            payload.append(
                {
                    "relative_path": page.get("relative_path"),
                    "regions": [
                        {
                            "text": region.get("text", ""),
                            "speaker_id": region.get("speaker_id"),
                            "speaker_name": region.get("speaker_name"),
                            "visual_evidence": region.get("visual_evidence", ""),
                        }
                        for region in (page.get("regions") or [])
                        if region.get("text")
                    ],
                }
            )
        prompt = (
            "Build a conservative chapter knowledge card from manga OCR and "
            "visual speaker evidence. Return JSON only: "
            "{\"characters\":[{\"character_id\":\"stable-id or NEW\","
            "\"name\":\"unknown if unconfirmed\",\"aliases\":[],"
            "\"description\":\"\",\"confidence\":0.0,"
            "\"source_refs\":[{\"relative_path\":\"...\",\"region_id\":0}]}],"
            "\"terms\":[{\"source\":\"\",\"target\":\"\",\"category\":\"\","
            "\"confidence\":0.0,\"source_refs\":[]}],"
            "\"relationships\":[],\"voice_profiles\":[],"
            "\"chapter_summary\":\"\"}. "
            "Never promote an uncertain identification to a confirmed fact. "
            "Every claim needs a page and region source. "
            f"Chapter: {chapter_name}. Source language: {source_language}. "
            f"Known characters: {known_characters}. Pages: {payload}"
        )
        return await self.client.chat_json(
            [
                {
                    "role": "system",
                    "content": (
                        "You maintain a source-grounded manga translation "
                        "knowledge base. Uncertainty is preferable to invention."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=8192,
        )
