"""fusion-osagent: Desktop Embodied AI barrier layer."""

from __future__ import annotations

__version__ = "1.1.0rc1"

from os_agent.api import DesktopAgent
from os_agent.config import OsaConfig

__all__ = ["DesktopAgent", "OsaConfig", "__version__"]
