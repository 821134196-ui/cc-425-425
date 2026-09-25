"""岗位与权限。

虚构平台四个岗位：
- ADMIN     管理员：规则版本、全部复核与退款权限
- OPERATOR  岗亭员：登记进出场、人工放行、处理复核案件
- AUDITOR   稽核员：只读全部台账（不能放行/退款/改规则）
- FINANCE   财务员：退款台账与执行退款（不能放行）
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

# 动作 -> 允许的岗位
PERMISSIONS: dict[str, set[str]] = {
    "lane.view": {"ADMIN", "OPERATOR", "AUDITOR", "FINANCE"},
    "record.view": {"ADMIN", "OPERATOR", "AUDITOR", "FINANCE"},
    "message.ingest": {"ADMIN", "OPERATOR"},
    "payment.bind": {"ADMIN", "OPERATOR"},
    "rule.view": {"ADMIN", "OPERATOR", "AUDITOR", "FINANCE"},
    "rule.create": {"ADMIN"},
    "release.create": {"ADMIN", "OPERATOR"},
    "review.view": {"ADMIN", "OPERATOR", "AUDITOR", "FINANCE"},
    "review.resolve": {"ADMIN", "OPERATOR"},
    "refund.view": {"ADMIN", "AUDITOR", "FINANCE"},
    "refund.create": {"ADMIN", "FINANCE"},
}

ROLE_NAMES = {
    "ADMIN": "管理员",
    "OPERATOR": "岗亭员",
    "AUDITOR": "稽核员",
    "FINANCE": "财务员",
}


class PermissionError_(Exception):
    """岗位无权执行该动作。"""


@dataclass(frozen=True)
class Operator:
    id: int
    login: str
    display_name: str
    role: str

    def can(self, action: str) -> bool:
        return self.role in PERMISSIONS.get(action, set())

    def require(self, action: str) -> None:
        if not self.can(action):
            raise PermissionError_(
                f"{ROLE_NAMES[self.role]}（{self.role}）无权执行 {action}"
            )


def authenticate(conn: sqlite3.Connection, login: str, pass_code: str) -> Operator | None:
    row = conn.execute(
        "SELECT id, login, display_name, role FROM operators "
        "WHERE login = ? AND pass_code = ?",
        (login, pass_code),
    ).fetchone()
    if row is None:
        return None
    return Operator(**dict(row))


def get_operator(conn: sqlite3.Connection, operator_id: int) -> Operator | None:
    row = conn.execute(
        "SELECT id, login, display_name, role FROM operators WHERE id = ?",
        (operator_id,),
    ).fetchone()
    return Operator(**dict(row)) if row else None
