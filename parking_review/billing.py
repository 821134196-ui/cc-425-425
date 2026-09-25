"""计费引擎：免费时长 + 按小时计费 + 单日封顶 + 跨日切片 + 优惠券 + 版本快照。

全部金额以整数「分」计算，避免浮点误差。规则：

1. 停车时长按入场/出场设备时间之差计整分钟；
2. 免费时长只在入场当日抵扣一次；
3. 免费时长外按「每进 1 小时」计费（不足 1 小时按 1 小时）；
4. 每个自然日（00:00–24:00）的费用单独受当日封顶限制；
5. 优惠券在出场时点校验有效性，现金券定额抵扣，折扣券按百分比减免。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from . import timeutil


class CouponInvalid(Exception):
    """优惠券失效，code 取值：expired/not_started/used/voided/plate_mismatch。"""

    LABEL = {
        "expired": "已过期",
        "not_started": "未到生效时间",
        "used": "已被使用",
        "voided": "已作废",
        "plate_mismatch": "车牌不匹配",
    }

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def yuan(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def ceil_unit(minutes: int, unit: int = 60) -> int:
    if minutes <= 0:
        return 0
    return (minutes + unit - 1) // unit


def split_day_slices(entry: datetime, exit_dt: datetime) -> list[dict[str, Any]]:
    """按自然日切分停车区间，返回 [{date, minutes}]，含跨日片段。"""
    slices: list[dict[str, Any]] = []
    cursor = entry
    while cursor < exit_dt:
        day_end = datetime(cursor.year, cursor.month, cursor.day) + timedelta(days=1)
        seg_end = min(day_end, exit_dt)
        minutes = int(round((seg_end - cursor).total_seconds() / 60))
        slices.append({"date": cursor.strftime("%Y-%m-%d"), "minutes": minutes})
        cursor = seg_end
    return slices


@dataclass
class Rule:
    id: int
    version: int
    effective_from: str
    effective_to: str | None
    free_minutes: int
    hour_fee_cents: int
    daily_cap_cents: int
    note: str | None = None

    @classmethod
    def from_row(cls, row: Any) -> "Rule":
        return cls(
            id=row["id"],
            version=row["version"],
            effective_from=row["effective_from"],
            effective_to=row["effective_to"],
            free_minutes=row["free_minutes"],
            hour_fee_cents=row["hour_fee_cents"],
            daily_cap_cents=row["daily_cap_cents"],
            note=row["note"],
        )


def select_rule_version(conn: Any, lot_id: int, entry_time: str) -> Rule:
    """按入场时间选择适用规则版本：effective_from <= entry_time < effective_to。"""
    row = conn.execute(
        """
        SELECT * FROM rule_version
        WHERE lot_id = ?
          AND effective_from <= ?
          AND (effective_to IS NULL OR effective_to > ?)
        ORDER BY version DESC
        """,
        (lot_id, entry_time, entry_time),
    ).fetchone()
    if row is None:
        raise ValueError(f"停车场 {lot_id} 在 {entry_time} 没有生效的计费规则版本")
    return Rule.from_row(row)


def compute_fee(entry_time: str, exit_time: str, rule: Rule) -> dict[str, Any]:
    """计算基础车费（未扣券），并返回逐日明细分片。"""
    entry = timeutil.parse(entry_time)
    exit_dt = timeutil.parse(exit_time)
    if exit_dt < entry:
        raise ValueError("出场时间早于入场时间")

    duration = int(round((exit_dt - entry).total_seconds() / 60))
    raw_slices = split_day_slices(entry, exit_dt)

    total_fee = 0
    detail: list[dict[str, Any]] = []
    free_left = rule.free_minutes
    for idx, sl in enumerate(raw_slices):
        minutes = sl["minutes"]
        free_deducted = min(free_left, minutes)
        free_left -= free_deducted
        billable = minutes - free_deducted
        units = ceil_unit(billable, 60)
        raw_fee = units * rule.hour_fee_cents
        capped = min(raw_fee, rule.daily_cap_cents)
        total_fee += capped
        detail.append(
            {
                "date": sl["date"],
                "day_index": idx + 1,
                "minutes": minutes,
                "free_deducted_minutes": free_deducted,
                "billable_minutes": billable,
                "hour_units": units,
                "raw_fee_cents": raw_fee,
                "fee_cents": capped,
                "cap_applied": raw_fee > rule.daily_cap_cents,
            }
        )

    return {
        "duration_minutes": duration,
        "base_fee_cents": total_fee,
        "cross_day": len(detail) > 1,
        "slices": detail,
    }


def validate_coupon(coupon: Any, exit_time: str, plate: str | None) -> None:
    """coupon 为 sqlite3.Row；无效即抛 CouponInvalid。"""
    if coupon["status"] == "used":
        raise CouponInvalid("used", f"优惠券 {coupon['code']} 已用于订单")
    if coupon["status"] == "void":
        raise CouponInvalid("voided", f"优惠券 {coupon['code']} 已作废")
    if exit_time < coupon["valid_from"]:
        raise CouponInvalid(
            "not_started",
            f"优惠券 {coupon['code']} {coupon['valid_from']} 起才生效",
        )
    if exit_time > coupon["valid_to"]:
        raise CouponInvalid(
            "expired", f"优惠券 {coupon['code']} 已于 {coupon['valid_to']} 过期"
        )
    bound_plate = coupon["plate"]
    if bound_plate and bound_plate != plate:
        raise CouponInvalid(
            "plate_mismatch",
            f"优惠券 {coupon['code']} 仅限 {bound_plate} 使用，当前车辆 {plate or '无牌'}",
        )


def apply_coupon(fee_cents: int, coupon: Any) -> int:
    """返回抵扣金额（分）。调用前应先 validate_coupon。"""
    if coupon["type"] == "cash":
        return min(fee_cents, int(coupon["amount"]))
    # discount：amount 存百分点，如 20 = 减 20%，四舍五入到分
    pct = int(coupon["amount"])
    discount = int((fee_cents * pct + 50) // 100)
    return min(fee_cents, discount)


def build_snapshot(
    *,
    rule: Rule,
    lot_name: str,
    plate: str | None,
    entry_time: str,
    exit_time: str,
    fee_detail: dict[str, Any],
    coupon: Any | None,
    discount_cents: int,
) -> dict[str, Any]:
    """构造写入订单的不可变计费快照（订单结算后不随规则修改重算）。"""
    payable = fee_detail["base_fee_cents"] - discount_cents
    return {
        "lot_name": lot_name,
        "plate": plate,
        "entry_time": entry_time,
        "exit_time": exit_time,
        "duration_minutes": fee_detail["duration_minutes"],
        "rule": {
            "rule_version_id": rule.id,
            "version": rule.version,
            "effective_from": rule.effective_from,
            "effective_to": rule.effective_to,
            "free_minutes": rule.free_minutes,
            "hour_fee_cents": rule.hour_fee_cents,
            "hour_fee_yuan": yuan(rule.hour_fee_cents),
            "daily_cap_cents": rule.daily_cap_cents,
            "daily_cap_yuan": yuan(rule.daily_cap_cents),
        },
        "day_slices": fee_detail["slices"],
        "cross_day": fee_detail["cross_day"],
        "base_fee_cents": fee_detail["base_fee_cents"],
        "coupon": (
            None
            if coupon is None
            else {
                "code": coupon["code"],
                "type": coupon["type"],
                "amount": coupon["amount"],
                "valid_from": coupon["valid_from"],
                "valid_to": coupon["valid_to"],
            }
        ),
        "discount_cents": discount_cents,
        "payable_cents": payable,
        "snapshot_note": "已结算订单按此快照计费，规则后续修改不重算",
    }
