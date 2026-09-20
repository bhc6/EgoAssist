"""EgoAssist full-duplex embodied assistance framework.

Core runtime: observation, interaction, and memory coordinated by application/.
Offline capture/retrieval benchmarks are separate research entry points.
"""

from pathlib import Path

from .config import Config

PACKAGE_ROOT = Path(__file__).parent
CONSOLE_ROOT = PACKAGE_ROOT / "console"
BUILTIN_SKILLS_ROOT = PACKAGE_ROOT / "skills"

__all__ = ["BUILTIN_SKILLS_ROOT", "CONSOLE_ROOT", "Config", "PACKAGE_ROOT"]
__version__ = "0.1.0"
