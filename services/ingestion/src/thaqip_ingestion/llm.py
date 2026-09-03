"""Model gateway (PRD architecture: provider-agnostic, structured outputs only).

Configured via env:
  THAQIP_ANTHROPIC_API_KEY   product API key (never reuse tooling credentials)
  THAQIP_LLM_MODEL           default claude-haiku-4-5-20251001 (cheap extraction lane)

Every task returns structured data and is designed for corpus-level caching by
the caller. Golden-set evals gate any model/prompt change (M3-2 quality bar).
"""
from __future__ import annotations

import json
import logging
import os

import httpx

log = logging.getLogger("thaqip.llm")

EXTRACT_SYSTEM = """أنت محلل عطاءات حكومية سعودية. استخرج من نص كراسة الشروط قائمة المتطلبات الإلزامية على المتنافس.
أعد JSON فقط: قائمة عناصر بالشكل:
{"requirement": "...", "category": "document|guarantee|qualification|deadline|technical",
 "source_ref": "اقتباس قصير أو رقم البند", "confidence": 0.0-1.0}
لا تخترع متطلبات غير واردة في النص. أعد [] إذا لم تجد شيئاً."""


class LLMGateway:
    def __init__(self, api_key: str, model: str) -> None:
        self._model = model
        self._http = httpx.AsyncClient(
            base_url="https://api.anthropic.com",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            timeout=120,
        )

    @classmethod
    def from_env(cls) -> LLMGateway | None:
        key = os.environ.get("THAQIP_ANTHROPIC_API_KEY")
        if not key:
            return None
        return cls(key, os.environ.get("THAQIP_LLM_MODEL", "claude-haiku-4-5-20251001"))

    async def extract_compliance(self, tsd_text: str) -> list[dict]:
        resp = await self._http.post("/v1/messages", json={
            "model": self._model,
            "max_tokens": 4000,
            "system": EXTRACT_SYSTEM,
            "messages": [{"role": "user", "content": tsd_text[:120_000]}],
        })
        resp.raise_for_status()
        text = "".join(b.get("text", "") for b in resp.json().get("content", []))
        try:
            start, end = text.find("["), text.rfind("]") + 1
            items = json.loads(text[start:end])
            return [it for it in items if isinstance(it, dict) and it.get("requirement")]
        except (ValueError, json.JSONDecodeError):
            log.warning("extract_compliance: unparseable model output")
            return []
