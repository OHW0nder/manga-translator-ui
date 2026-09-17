from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .providers import OpenAICompatibleClient, image_data_uri


@dataclass(slots=True)
class VisionAnalyzer:
    client: OpenAICompatibleClient

    async def correct_ocr(
        self,
        image_path: str | Path,
        page_payload: dict[str, Any],
        *,
        source_language: str,
    ) -> list[dict[str, Any]]:
        regions = page_payload.get("regions") or []
        if not regions:
            return []
        compact = [
            {
                "region_id": index,
                "bbox": _bbox(region),
                "text": region.get("text", ""),
                "confidence": region.get(
                    "ocr_confidence", region.get("prob", 1.0)
                ),
            }
            for index, region in enumerate(regions)
        ]
        prompt = (
            "You are correcting manga OCR. Use only visible pixels. "
            "Return JSON {\"regions\":[{\"region_id\":0,\"text\":\"...\","
            "\"confidence\":0.0}]}. Preserve the original language; do not "
            "translate. If text is unreadable, keep the OCR text and lower "
            f"confidence. Source language: {source_language}. Regions: "
            f"{compact}"
        )
        result = await self.client.chat_json(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": image_data_uri(image_path)},
                        },
                    ],
                }
            ],
            temperature=0,
        )
        corrections = result.get("regions")
        return corrections if isinstance(corrections, list) else []

    async def attribute_speakers(
        self,
        image_path: str | Path,
        page_payload: dict[str, Any],
        *,
        known_characters: list[dict[str, Any]],
        source_language: str,
    ) -> dict[str, Any]:
        regions = page_payload.get("regions") or []
        compact_regions = [
            {
                "region_id": index,
                "bbox": _bbox(region),
                "text": region.get("text", ""),
            }
            for index, region in enumerate(regions)
            if str(region.get("text") or "").strip()
        ]
        compact_characters = [
            {
                "character_id": item.get("id") or item.get("character_id"),
                "name": item.get("confirmed_name")
                or item.get("canonical_name")
                or item.get("name"),
                "aliases": [
                    alias.get("alias")
                    for alias in (item.get("aliases") or [])
                    if isinstance(alias, dict) and alias.get("alias")
                ][:4],
            }
            for item in known_characters
            if isinstance(item, dict)
        ]
        prompt = (
            "Identify the visible speaker for each manga speech region. "
            "Do not explain or reason step by step. Return compact JSON only: "
            "{\"regions\":[{\"region_id\":0,\"speaker_id\":\"stable-id\","
            "\"speaker_name\":\"name or unknown\",\"confidence\":0.0,"
            "\"speech_type\":\"dialogue|thought|narration|sfx|sign|unknown\","
            "\"visual_evidence\":\"at most 12 words\"}],"
            "\"new_characters\":[{\"character_id\":\"stable-id\","
            "\"name\":\"unknown\",\"aliases\":[],\"confidence\":0.0}]}. "
            "Match an existing character id whenever the visible identity "
            "matches. Do not guess names from appearance. Use unknown and low "
            "confidence when identity is unclear. "
            f"Source language: {source_language}. "
            f"Known characters: {compact_characters}. "
            f"Regions: {compact_regions}"
        )
        return await self.client.chat_json(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": image_data_uri(image_path)},
                        },
                    ],
                }
            ],
            temperature=0,
            max_tokens=min(
                1536,
                max(256, 128 + len(compact_regions) * 64),
            ),
        )


def _bbox(region: dict[str, Any]) -> list[float] | None:
    lines = region.get("lines")
    if not isinstance(lines, list) or not lines:
        return None
    points = [
        point
        for line in lines
        for point in line
        if isinstance(point, (list, tuple)) and len(point) >= 2
    ]
    if not points:
        return None
    try:
        xs = [float(point[0]) for point in points]
        ys = [float(point[1]) for point in points]
    except (TypeError, ValueError):
        return None
    return [min(xs), min(ys), max(xs), max(ys)]
