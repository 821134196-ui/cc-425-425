"""进程内模拟支付适配器（不发起任何真实网络请求）。

车辆的 gateway_script 决定脚本行为：
- ok        ：支付成功
- fail_once ：首次失败、重试成功（模拟余额不足后补扣）
- declined  ：始终拒绝（模拟卡片注销）
"""
from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass
class PayResult:
    success: bool
    gateway_no: str | None = None
    reason: str | None = None


class InMemoryPaymentGateway:
    """线程安全的内存支付网关。退款同样在此模拟。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._fail_count: dict[str, int] = {}
        self._paid_orders: dict[str, str] = {}      # order_no -> 支付流水
        self._refunded: dict[str, str] = {}         # order_no -> 退款流水
        self._seq = 0

    def _next_no(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}{202609250000 + self._seq}"

    def pay(self, order_no: str, method: str, script: str, amount_cents: int) -> PayResult:
        with self._lock:
            if order_no in self._paid_orders:
                # 幂等：已支付订单直接返回既有流水，绝不重复扣款
                return PayResult(True, self._paid_orders[order_no], "订单已支付，幂等返回")
            if script == "declined":
                return PayResult(False, None, "支付渠道拒绝：签约已注销（模拟）")
            if script == "fail_once":
                count = self._fail_count.get(order_no, 0)
                self._fail_count[order_no] = count + 1
                if count == 0:
                    return PayResult(False, None, "余额不足，首次扣款失败（模拟）")
            gw_no = self._next_no("PAY")
            self._paid_orders[order_no] = gw_no
            return PayResult(True, gw_no, None)

    def refund(self, order_no: str, amount_cents: int) -> PayResult:
        with self._lock:
            if order_no not in self._paid_orders:
                return PayResult(False, None, "原支付不存在，无法退款")
            if order_no in self._refunded:
                # 退款幂等：返回既有退款流水
                return PayResult(True, self._refunded[order_no], "退款已存在，幂等返回")
            gw_no = self._next_no("RFD")
            self._refunded[order_no] = gw_no
            return PayResult(True, gw_no, None)

    def reset(self) -> None:
        with self._lock:
            self._fail_count.clear()
            self._paid_orders.clear()
            self._refunded.clear()
            self._seq = 0


# 全局单例（进程内）
gateway = InMemoryPaymentGateway()
