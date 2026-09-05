"""fusion-mlx adapter: local VLM inference via fusion-core.FusionMLXClient.

Endpoint http://localhost:11434 (OpenAI-compatible), api key dahai168.
Fast/Slow dual-core model selection lives in reasoning.py; this adapter is a
thin inference wrapper returning raw text/JSON for a given image+prompt.

JSON extraction is fail-loud (D5 fix): chat_json returns None when the model
output cannot be parsed, so callers distinguish "no answer" from a legitimate
empty object and never silently click (0,0). The stub mirrors the real adapter's
contract so tests exercise the real parsing path.
"""
from __future__ import annotations

import asyncio
import json

from fusion_core import FusionMLXClient, get_logger

from os_agent.circuit_breaker import BreakerConfig, CircuitBreaker, CircuitOpenError
from os_agent.config import OsaConfig
from os_agent.coordination import ClusterHealth, build_cluster_health

log = get_logger("os_agent.adapters.mlx")


class MlxAdapter:
    name = "mlx"

    def __init__(self, cfg: OsaConfig, model: str | None = None) -> None:
        self.cfg = cfg
        self.model = model or cfg.fast_model
        self._client: FusionMLXClient | None = None
        # A4: bound concurrent mlx vision inferences. Multiple decide()/locate()
        # calls in one event loop (and multiple DesktopAgent instances sharing a
        # mlx cluster) otherwise fire N×M simultaneous requests → mlx OOM.
        # The semaphore serializes at the inference boundary only.
        self._semaphore = asyncio.Semaphore(max(1, cfg.vlm_concurrency))
        # Gap 3: cluster-level circuit breaker. A4 bounds concurrency but a
        # DOWN mlx cluster still gets every request fired at it (each waiting
        # the full timeout, pinning every slot). The breaker opens after
        # consecutive failures / high failure rate and fast-fails until the
        # cluster cools down — protects the cluster, not just this instance.
        self.breaker = CircuitBreaker(
            name="mlx",
            cfg=BreakerConfig(
                failure_threshold=cfg.breaker_failure_threshold,
                window_s=cfg.breaker_window_s,
                failure_rate=cfg.breaker_failure_rate,
                cooldown_s=cfg.breaker_cooldown_s,
                min_calls_for_rate=cfg.breaker_min_calls_for_rate,
            ),
        )
        # Gap 2: cluster-level health. Per-instance breaker only sees this
        # node's failures; N nodes each below their own threshold still flood a
        # struggling cluster. The shared ClusterHealth aggregates failures
        # across all nodes on this host so any node opens when the CLUSTER is
        # sick, not just when it itself is. node_id set by DesktopAgent.
        self.node_id = "osagent"
        self.cluster_health: ClusterHealth | None = build_cluster_health()

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
        # Gap 3: fast-fail when the local breaker is OPEN instead of firing
        # another request at a down cluster and pinning a slot for the full
        # timeout.
        if not self.breaker.allow():
            log.warning("mlx breaker OPEN — fast-failing chat_vision")
            raise CircuitOpenError("mlx circuit breaker is open")
        # Gap 2: also fast-fail when the CLUSTER aggregate signal is open —
        # another node may have hit the threshold even if this node did not.
        if self.cluster_health is not None and self.cluster_health.should_open():
            log.warning("mlx cluster health OPEN — fast-failing chat_vision")
            self.breaker.on_failure()
            raise CircuitOpenError("mlx cluster health is open")
        client = self._ensure()
        use_model = model or self.model
        content = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
        ]
        try:
            # A2: bound the inference so a stuck mlx (model load / OOM / GPU
            # stall) cannot hang the Agent loop forever. Re-raise on timeout so
            # callers (fail-loud) treat it as a failed inference, not a silent
            # default coordinate.
            # A4: acquire the concurrency semaphore so concurrent decide()/locate()
            # calls queue instead of flooding mlx (OOM under cluster load).
            async with self._semaphore:
                resp = await asyncio.wait_for(
                    client.chat(messages=[{"role": "user", "content": content}], model=use_model),
                    timeout=self.cfg.vlm_timeout,
                )
            self.breaker.on_success()
            if self.cluster_health is not None:
                self.cluster_health.report_success(self.node_id)
            return resp.content.strip()
        except TimeoutError:
            log.error("mlx vision chat timed out after %.1fs model=%s", self.cfg.vlm_timeout, use_model)
            self.breaker.on_failure()
            if self.cluster_health is not None:
                self.cluster_health.report_failure(self.node_id)
            raise
        except Exception:
            log.exception("mlx vision chat failed")
            self.breaker.on_failure()
            if self.cluster_health is not None:
                self.cluster_health.report_failure(self.node_id)
            raise

    async def chat_json(self, prompt: str, image_b64: str, model: str | None = None) -> dict | None:
        """Return parsed JSON dict, or None if the model output is not valid JSON.

        None (not {}) is the fail-loud signal: callers must treat None as a
        failed inference and never use a default coordinate.
        """
        raw = await self.chat_vision(prompt, image_b64, model)
        obj = _extract_json(raw)
        if obj is None:
            log.warning("mlx returned non-JSON: %s", raw[:200])
        return obj

    async def health(self) -> bool:
        try:
            await self._ensure().health()
            return True
        except Exception as e:
            log.warning("mlx health failed: %s", e)
            return False

    async def close(self) -> None:
        client = self._client
        self._client = None
        if client is not None and hasattr(client, "aclose"):
            try:
                await client.aclose()
            except Exception as e:
                log.warning("mlx client close raised: %s", e)
        elif client is not None and hasattr(client, "close"):
            try:
                client.close()
            except Exception as e:
                log.warning("mlx client close raised: %s", e)
        log.info("mlx adapter closed")


def _extract_json(raw: str) -> dict | None:
    """Robust balanced-brace JSON extraction. Returns None on failure.

    R1: real VL models (esp. 7B fast) often prefix prose like "Sure, for
    {reason} here is the JSON: ```json\\n{...}\\n```". Taking the first '{'
    lands inside the prose and the balanced-brace candidate is not valid JSON
    -> None -> fail-loud -> permanent force-escalate to slow. Prefer a fenced
    ```json ... ``` block when present, then fall back to the first '{'.
    """
    if not raw:
        return None
    fenced = _extract_fenced_json(raw)
    if fenced is not None:
        return fenced
    start = raw.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(raw)):
        ch = raw[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = raw[start : i + 1]
                try:
                    parsed = json.loads(candidate)
                    return parsed if isinstance(parsed, dict) else None
                except json.JSONDecodeError:
                    return None
    return None


def _extract_fenced_json(raw: str) -> dict | None:
    """Parse the first ```json ... ``` (or bare ``` ... ```) fenced block."""
    import re

    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if m is None:
        return None
    try:
        parsed = json.loads(m.group(1))
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


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
        low = prompt.lower()
        # grounding/locate prompt -> return a normalized coordinate
        if "grounding model" in low or "normalized fractions" in low or "find the element" in low:
            return json.dumps({"x": 0.5, "y": 0.5, "confidence": 0.9, "label": "stub-target"})
        # fast action prompt -> a routine click
        if "coordinate" in low or "click" in low:
            return json.dumps({"x": 0.5, "y": 0.5, "action": "click", "confidence": 0.9})
        return json.dumps({"action": "none", "reason": "stub"})

    async def chat_json(self, prompt: str, image_b64: str, model: str | None = None) -> dict | None:
        raw = await self.chat_vision(prompt, image_b64, model)
        return _extract_json(raw)

    async def health(self) -> bool:
        return True

    async def close(self) -> None:
        log.info("stub mlx closed")
