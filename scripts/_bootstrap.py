"""脚本公共引导：把仓库根加进 sys.path，并构造应用容器。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # 允许 `python scripts/xxx.py` 直接运行
    sys.path.insert(0, str(ROOT))

from config.settings import Settings, get_settings  # noqa: E402
from core.deps import Container, build_container, set_container  # noqa: E402


def bootstrap(settings: Settings | None = None) -> Container:
    settings = settings or get_settings()
    settings.ensure_dirs()
    return set_container(build_container(settings))


__all__ = ["ROOT", "bootstrap"]
