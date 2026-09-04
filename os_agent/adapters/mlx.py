"""fusion-mlx adapter: local VLM inference via fusion-core.FusionMLXClient.

Endpoint http://localhost:11434 (OpenAI-compatible), api key dahai168.
Fast/Slow dual-core model selection lives in reasoning.py; this adapter is a
thin inference wrapper returning raw text/JSON for a given image+prompt.
"""
from __future__ import annotations

import json

from fusion_core import FusionMLXClient, get_logger

from os_agent.config import OsaConfig

log = get_logger("os_agent.adapters.mlx")


class MlxAdapter:
    name = "mlx"

    def __init__(self, cfg: OsaConfig, model: str | None = None) -> None:
        self.cfg = cfg
        self.model = model or cfg.fast_model
        self._client: FusionMLXClient | None = None

    def _ensure(self) -> FusionMLXClient:
        if self._client is None:
            log.info("mlx connect url=%s model=%s", self.cfg.fusion_mlx_url, self.model)
            self._client = FusionMLXClient(
                base_url=self.cfg.fusion_mlx_url,
                api_key=self.cfg.fusion_mlx_api_key,
                model=self.model,
            )
        return self._client

    async def chat_vision(self, prompt: str, image_b64: str, model: str | None = None) -> str:
        client = self._ensure()
        use_model = model or self.model
        content = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
        ]
        try:
            resp = await client.chat(messages=[{"role": "user", "content": content}], model=use_model)
            return resp.content.strip()
        except Exception as e:
            log.error("mlx vision chat failed: %s", e)
            raise

    async def chat_json(self, prompt: str, image_b64: str, model: str | None = None) -> dict:
        raw = await self.chat_vision(prompt, image_b64, model)
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end < 0:
            log.warning("mlx returned non-JSON: %s", raw[:200])
            return {}
        try:
            return json.loads(raw[start : end + 1])
        except json.JSONDecodeError as e:
            log.warning("mlx JSON parse failed: %s raw=%s", e, raw[:200])
            return {}

    async def health(self) -> bool:
        try:
            await self._ensure().health()
            return True
        except Exception as e:
            log.warning("mlx health failed: %s", e)
            return False

    async def close(self) -> None:
        self._client = None
        log.info("mlx adapter closed")


class StubMlxAdapter:
    name = "mlx-stub"

    def __init__(self, cfg: OsaConfig, model: str | None = None) -> None:
        self.cfg = cfg
        self.model = model or cfg.fast_model
        self.calls: list[dict] = []
        log.info("stub mlx ready model=%s", self.model)

    async def chat_vision(self, prompt: str, image_b64: str, model: str | None = None) -> str:
        rec = {"prompt": prompt[:120], "model": model or self.model}
        self.calls.append(rec)
        if "coordinate" in prompt.lower() or "click" in prompt.lower():
            return json.dumps({"x": 0.5, "y": 0.5, "action": "click", "confidence": 0.9})
        return json.dumps({"action": "none", "reason": "stub"})

    async def chat_json(self, prompt: str, image_b64: str, model: str | None = None) -> dict:
        raw = await self.chat_vision(prompt, image_b64, model)
        return json.loads(raw) if raw.startswith("{") else {}

    async def health(self) -> bool:
        return True

    async def close(self) -> None:
        log.info("stub mlx closed")
