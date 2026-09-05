"""F5.3 autotest acceptance loop — osagent executes, autotest asserts.

osagent is the execution end (drives GUI via DesktopAgent), fusion-autotest
is the assertion end (VLM-verifies screenshot vs PRD requirement). This
module orchestrates: run a plan step → capture frame → hand (screenshot,
expected) to `fusion-autotest vlm` → parse defect JSON → pass/fail.

Protocol (local, until upstream issue AT1 fixes a stable JSON contract):
- write screenshot PNG to a temp path
- invoke `fusion-autotest --config <cfg> vlm <shot> <prd> --output <dir>`
- read `<dir>/vlm_report.json` (or parse stdout) → DefectVerdict
- osagent never patches fusion-autotest; only shells out to its CLI.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from fusion_core import get_logger

from os_agent.config import OsaConfig

log = get_logger("os_agent.loops.autotest")


@dataclass
class DefectVerdict:
    ok: bool
    match: bool = False
    reason: str = ""
    defects: list[dict] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


class AutotestLoop:
    """Drive GUI + verify via fusion-autotest VLM assertion."""

    def __init__(self, cfg: OsaConfig, autotest_bin: str = "fusion-autotest", config_path: str = "config.toml") -> None:
        self.cfg = cfg
        self.autotest_bin = autotest_bin
        self.config_path = config_path

    def verify(self, shot_png_bytes: bytes, prd_path: str, expected: str | None = None) -> DefectVerdict:
        """Hand a screenshot + PRD to fusion-autotest vlm; parse verdict.

        Writes the screenshot to a temp PNG, shells out to `fusion-autotest vlm`,
        reads `<out>/vlm_result.json`. No defects → ok. `expected` is logged
        for traceability but autotest asserts against the full PRD requirement
        set (the upstream issue AT1 will add a single-requirement mode).
        """
        if not os.path.isfile(prd_path):
            log.error("autotest verify: PRD not found: %s", prd_path)
            return DefectVerdict(ok=False, reason=f"prd not found: {prd_path}")
        with tempfile.TemporaryDirectory(prefix="osa_autotest_") as tmp:
            shot_path = str(Path(tmp) / "shot.png")
            Path(shot_path).write_bytes(shot_png_bytes)
            out_dir = str(Path(tmp) / "out")
            try:
                proc = self._run_vlm(shot_path, prd_path, out_dir)
            except FileNotFoundError as e:
                log.error("autotest bin missing: %s", e)
                return DefectVerdict(ok=False, reason=f"autotest bin missing: {e}")
            if proc.returncode != 0:
                log.error("autotest vlm exit %d: %s", proc.returncode, proc.stderr[:300] if proc.stderr else "")
                return DefectVerdict(ok=False, reason=f"vlm exit {proc.returncode}")
            verdict = self._parse_verdict(out_dir, proc.stdout or "")
            verdict.raw["expected"] = expected
            log.info("autotest verify: ok=%s defects=%d expected=%r", verdict.ok, len(verdict.defects), expected)
            return verdict

    def _run_vlm(self, shot_path: str, prd_path: str, out_dir: str) -> subprocess.CompletedProcess:
        cmd = [
            self.autotest_bin, "--config", self.config_path,
            "vlm", shot_path, prd_path, "--output", out_dir,
        ]
        log.info("autotest vlm: %s", " ".join(cmd))
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120)

    async def verify_async(self, shot_png_bytes: bytes, prd_path: str, expected: str | None = None) -> DefectVerdict:
        """R2: async wrapper — offloads the blocking verify() to a worker thread.

        verify() shells out to `fusion-autotest vlm` with subprocess.run(timeout=120),
        which blocks the event loop for up to 2 minutes if called from a coroutine.
        asyncio.to_thread lets the Agent loop keep running screenshots/clicks while
        the subprocess blocks in a worker thread.
        """
        import asyncio

        return await asyncio.to_thread(self.verify, shot_png_bytes, prd_path, expected)

    def _parse_verdict(self, out_dir: str, stdout: str) -> DefectVerdict:
        result_path = Path(out_dir) / "vlm_result.json"
        if not result_path.is_file():
            log.warning("autotest: vlm_result.json missing; stdout=%s", stdout[:200])
            return DefectVerdict(ok=False, reason="vlm_result.json missing")
        try:
            data = json.loads(result_path.read_text())
        except json.JSONDecodeError as e:
            log.error("autotest: vlm_result.json parse failed: %s", e)
            return DefectVerdict(ok=False, reason=f"json parse: {e}")
        defects = data.get("defects") or []
        return DefectVerdict(
            ok=len(defects) == 0,
            match=len(defects) == 0,
            reason="no defects" if not defects else f"{len(defects)} defects",
            defects=defects,
            raw={"requirements_count": data.get("requirements_count", 0)},
        )
