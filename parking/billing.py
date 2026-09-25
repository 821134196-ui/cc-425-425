"""计费引擎（纯函数）。

规则按 *入场时间* 选取生效版本；费用计算结果由上层序列化进订单快照，
规则之后修改或新增版本都不会影响已结算订单。

计费口径（虚构示例）：
1. 免费时长 + 优惠券赠送分钟，从入场所在自然日顺序扣减；
2. 每个自然日独立按计费单位向上取整，并适用当日封顶金额；
3. 立减券在总金额上扣减，最低 0 分。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

DAY_CAP = "daily_cap"


@dataclass(frozen=True)
class Rule:
    id: int
    version: int
    free_minutes: int
    unit_minutes: int
    unit_price_fen: int
    daily_cap_fen: int

    def to_snapshot(self) -> dict:
        return {
            "rule_version_id": self.id,
            "version": self.version,
            "free_minutes": self.free_minutes,
            "unit_minutes": self.unit_minutes,
            "unit_price_fen": self.unit_price_fen,
            "daily_cap_fen": self.daily_cap_fen,
        }


@dataclass(frozen=True)
class Coupon:
    id: int
    code: str
    coupon_type: str          # FREE_MINUTES | FIXED
    amount: int               # 分钟 或 分
    valid_from: datetime
    valid_to: datetime       # 含端点

    def usable_at(self, moment: datetime) -> bool:
        return self.valid_from <= moment <= self.valid_to


def parse_dt(value: str) -> datetime:
    """解析 ISO 时间（设备时间带时区偏移）。"""
    return datetime.fromisoformat(value)


def ceil_div(a: int, b: int) -> int:
    if a <= 0:
        return 0
    return -(-a // b)


def split_natural_days(entry: datetime, exit_dt: datetime) -> list[tuple[datetime, datetime]]:
    """按自然日（本地日历）切分停车区间。"""
    if exit_dt < entry:
        raise ValueError("exit time earlier than entry time")
    segments: list[tuple[datetime, datetime]] = []
    cursor = entry
    while cursor < exit_dt:
        next_noon_midnight = (cursor + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = min(next_noon_midnight, exit_dt)
        segments.append((cursor, end))
        cursor = end
    return segments


def _minutes(a: datetime, b: datetime) -> int:
    return int((b - a).total_seconds() // 60)


def compute_fee(
    entry: datetime,
    exit_dt: datetime,
    rule: Rule,
    coupon: Coupon | None = None,
    coupon_honored: bool = True,
) -> dict:
    """返回完整计费过程字典（作为订单 fee_detail 快照）。

    coupon_honored=False 表示顾客出示的券已失效：仍记录在明细中，但不抵扣。
    """
    duration_min = _minutes(entry, exit_dt)
    coupon_free_minutes = 0
    coupon_fixed_fen = 0
    coupon_record = None
    if coupon is not None:
        valid = coupon_honored and coupon.usable_at(exit_dt)
        coupon_record = {
            "coupon_id": coupon.id,
            "code": coupon.code,
            "type": coupon.coupon_type,
            "amount": coupon.amount,
            "applied": bool(valid),
            "reason": "APPLIED" if valid else "INVALID_OR_EXPIRED",
        }
        if valid:
            if coupon.coupon_type == "FREE_MINUTES":
                coupon_free_minutes = coupon.amount
            elif coupon.coupon_type == "FIXED":
                coupon_fixed_fen = coupon.amount

    total_free = rule.free_minutes + coupon_free_minutes
    remaining_free = total_free
    day_details = []
    amount_before_coupon = 0

    for seg_start, seg_end in split_natural_days(entry, exit_dt):
        seg_minutes = _minutes(seg_start, seg_end)
        free_used = min(remaining_free, seg_minutes)
        remaining_free -= free_used
        billed_minutes = seg_minutes - free_used
        units = ceil_div(billed_minutes, rule.unit_minutes)
        raw_fee = units * rule.unit_price_fen
        capped = False
        day_fee = raw_fee
        if rule.daily_cap_fen > 0 and day_fee > rule.daily_cap_fen:
            day_fee = rule.daily_cap_fen
            capped = True
        amount_before_coupon += day_fee
        day_details.append({
            "date": seg_start.date().isoformat(),
            "segment_minutes": seg_minutes,
            "free_minutes_used": free_used,
            "billed_minutes": billed_minutes,
            "units": units,
            "raw_fee_fen": raw_fee,
            "capped": capped,
            "fee_fen": day_fee,
        })

    total = max(0, amount_before_coupon - coupon_fixed_fen)
    return {
        "duration_minutes": duration_min,
        "free_minutes": rule.free_minutes,
        "coupon_free_minutes": coupon_free_minutes,
        "day_segments": day_details,
        "amount_before_coupon_fen": amount_before_coupon,
        "coupon_fixed_fen": coupon_fixed_fen,
        "coupon": coupon_record,
        "amount_fen": total,
        "cross_day": len({d["date"] for d in day_details}) > 1,
    }


def snapshot_fee(fee_detail: dict, rule: Rule) -> str:
    """组合订单保存的 JSON 字符串：规则快照 + 计算明细。"""
    return json.dumps(
        {"rule": rule.to_snapshot(), "detail": fee_detail},
        ensure_ascii=False,
        sort_keys=True,
    )
