"""时间工具：统一使用朴素本地时间 ISO8601 字符串。"""
from __future__ import annotations

from datetime import datetime, timedelta

FMT = "%Y-%m-%dT%H:%M:%S"


def parse(s: str) -> datetime:
    return datetime.strptime(s, FMT)


def format_(dt: datetime) -> str:
    return dt.strftime(FMT)


def now_iso() -> str:
    return format_(datetime.now().replace(microsecond=0))


def minutes_between(a: str, b: str) -> int:
    """两个 ISO 时间相差的整分钟（四舍五入到秒后取整）。"""
    delta = parse(b) - parse(a)
    return int(round(delta.total_seconds() / 60))


def shift(iso: str, *, minutes: int = 0, seconds: int = 0) -> str:
    return format_(parse(iso) + timedelta(minutes=minutes, seconds=seconds))
