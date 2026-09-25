"""运行配置：SQLite 数据库与本地识别证据目录。

可通过环境变量覆盖，测试时指向临时目录。
"""
from __future__ import annotations

import os
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("PARKING_DATA_DIR", _BASE / "data"))
DB_PATH = DATA_DIR / "parking.db"
EVIDENCE_DIR = DATA_DIR / "evidence"
STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"

DEFAULT_PORT = int(os.environ.get("PARKING_PORT", "8000"))


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
