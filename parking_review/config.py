"""平台配置与路径。"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("PARKING_DATA_DIR", BASE_DIR / "data"))
DB_PATH = DATA_DIR / "parking.db"
EVIDENCE_DIR = DATA_DIR / "evidence"

# 角色权限矩阵
PERMISSIONS: dict[str, set[str]] = {
    "monitor": {"lane:view", "event:view", "case:view"},
    "reviewer": {
        "lane:view", "event:view", "case:view",
        "case:review", "release:create",
    },
    "cashier": {
        "lane:view", "event:view", "case:view",
        "refund:create",
    },
    "admin": {
        "lane:view", "lane:manage", "event:view", "case:view",
        "case:review", "release:create", "refund:create",
        "rule:publish", "vehicle:manage",
    },
}

ROLE_NAMES = {
    "monitor": "监控员",
    "reviewer": "复核员",
    "cashier": "收费员",
    "admin": "管理员",
}


def can(role: str, permission: str) -> bool:
    return permission in PERMISSIONS.get(role, set())
