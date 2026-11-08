"""Task-signal classification for sandbox mode selection (issue #9).

Rule-based, code-decided (no model in the loop): a task whose text touches
credentials, secrets, or the host identity surface should default to the vm
sandbox so an embodied agent never sees the real session. Callers use the
recommendation as the DEFAULT mode; an explicit OSA_SANDBOX_MODE overrides it.
"""

from __future__ import annotations

import re

from fusion_core import get_logger

log = get_logger("os_agent.sandbox")

# Signals that make host execution the wrong default. Word-boundary regexes,
# case-insensitive. Kept deliberately narrow: false "vm" recommendations cost
# a disposable guest boot; false "host" recommendations leak the session.
SANDBOX_SIGNALS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bpasswords?\b", re.I),
    re.compile(r"\bpassphrases?\b", re.I),
    re.compile(r"\bcredentials?\b", re.I),
    re.compile(r"\bapi[- ]?keys?\b", re.I),
    re.compile(r"\baccess[- ]?tokens?\b", re.I),
    re.compile(r"\bsecrets?\b", re.I),
    re.compile(r"\bssh\s+keys?\b", re.I),
    re.compile(r"\bkeychain\b", re.I),
    re.compile(r"\blogin\b", re.I),
    re.compile(r"\bsign[- ]?in\b", re.I),
    re.compile(r"\bsign[- ]?up\b", re.I),
    re.compile(r"\boAuth\b", re.I),
    re.compile(r"\bwallet\b", re.I),
    re.compile(r"\bpayment\s+(method|info|card)\b", re.I),
    re.compile(r"\bcredit\s+card\b", re.I),
    re.compile(r"\b2fa\b", re.I),
    re.compile(r"\botp\b", re.I),
    re.compile(r"\bclipp?board\b", re.I),
    re.compile(r"\bkeynote\b|\bfacetime\b|\bmessages\b|\bmail\.app\b", re.I),
    re.compile(r"~/\.ssh\b", re.I),
)


def recommended_sandbox_mode(task_text: str) -> str:
    """Return "vm" when the task text carries a host-identity signal, else "host".

    This is a default-mode recommendation, not an enforcement gate: the caller
    decides with it (issue #9 item 3 — policy hook). Empty/None input is "host".
    """
    if not task_text:
        return "host"
    for pattern in SANDBOX_SIGNALS:
        if pattern.search(task_text):
            log.info("sandbox policy: task matched %r -> recommend vm", pattern.pattern)
            return "vm"
    return "host"
