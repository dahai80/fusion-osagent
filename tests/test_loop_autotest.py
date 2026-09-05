"""F5.3 autotest acceptance loop tests (offline, subprocess mocked)."""

from __future__ import annotations

import json
from pathlib import Path

from os_agent.config import OsaConfig
from os_agent.loops.autotest import AutotestLoop, DefectVerdict


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeAutotestLoop(AutotestLoop):
    def __init__(self, result: dict | None, returncode: int = 0):
        super().__init__(OsaConfig())
        self._result = result
        self._returncode = returncode
        self.calls: list[tuple] = []

    def _run_vlm(self, shot_path, prd_path, out_dir):
        self.calls.append((shot_path, prd_path, out_dir))
        if self._result is not None:
            Path(out_dir).mkdir(parents=True, exist_ok=True)
            Path(out_dir, "vlm_result.json").write_text(json.dumps(self._result))
        return FakeProc(returncode=self._returncode)


def _write_prd(tmp_path: Path) -> str:
    p = tmp_path / "prd.md"
    p.write_text("# PRD\n- button OK visible")
    return str(p)


def test_verify_no_defects_pass(tmp_path):
    prd = _write_prd(tmp_path)
    loop = FakeAutotestLoop(result={"requirements_count": 1, "defects": []})
    v = loop.verify(b"PNG-bytes", prd, expected="OK button")
    assert v.ok is True
    assert v.match is True
    assert len(loop.calls) == 1


def test_verify_with_defects_fail(tmp_path):
    prd = _write_prd(tmp_path)
    loop = FakeAutotestLoop(result={"requirements_count": 2, "defects": [{"severity": "major", "msg": "no OK button"}]})
    v = loop.verify(b"PNG-bytes", prd)
    assert v.ok is False
    assert len(v.defects) == 1
    assert "no OK button" in v.defects[0]["msg"]


def test_verify_prd_missing(tmp_path):
    loop = FakeAutotestLoop(result=None)
    v = loop.verify(b"PNG", "/no/such/prd.md")
    assert v.ok is False
    assert "prd not found" in v.reason
    assert len(loop.calls) == 0


def test_verify_vlm_exit_nonzero(tmp_path):
    prd = _write_prd(tmp_path)
    loop = FakeAutotestLoop(result=None, returncode=2)
    v = loop.verify(b"PNG", prd)
    assert v.ok is False
    assert "vlm exit 2" in v.reason


def test_verify_result_json_missing(tmp_path):
    prd = _write_prd(tmp_path)
    loop = FakeAutotestLoop(result=None, returncode=0)  # no json written
    v = loop.verify(b"PNG", prd)
    assert v.ok is False
    assert "vlm_result.json missing" in v.reason


def test_defectverdict_defaults():
    v = DefectVerdict(ok=True)
    assert v.defects == []
    assert v.raw == {}
    assert v.match is False
