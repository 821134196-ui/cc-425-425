"""进程内模拟支付适配器（不发起任何真实交易，全部为虚构数据）。

约定：支付令牌以 ``FAIL:`` 开头表示模拟失败；``OK:`` 表示成功。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ChargeResult:
    success: bool
    channel_no: str
    message: str


class InMemoryPaymentAdapter:
    """同一进程内记录所有扣款/退款请求，保证可重复测试。"""

    def __init__(self) -> None:
        self.charges: list[dict] = []
        self.refunds: list[dict] = []
        self._seq = 0

    def _next(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}2026{self._seq:06d}"

    def charge(self, order_no: str, token: str, amount_fen: int) -> ChargeResult:
        record = {"order_no": order_no, "token": token, "amount_fen": amount_fen}
        if not token:
            result = ChargeResult(False, "", "未绑定有效支付方式，无感扣费中止")
        elif token.startswith("FAIL:"):
            result = ChargeResult(
                False, self._next("FAIL"), f"模拟支付失败：发卡行拒绝（令牌 {token[-6:]}）"
            )
        elif amount_fen <= 0:
            result = ChargeResult(True, self._next("FREE"), "0 元订单免费放行")
        else:
            result = ChargeResult(True, self._next("PAY"), f"模拟扣款成功 {amount_fen} 分")
        record["result"] = result.success
        record["channel_no"] = result.channel_no
        self.charges.append(record)
        return result

    def refund(self, refund_no: str, order_no: str, token: str, amount_fen: int) -> ChargeResult:
        record = {"refund_no": refund_no, "order_no": order_no, "amount_fen": amount_fen}
        if amount_fen <= 0:
            result = ChargeResult(False, "", "退款金额必须大于 0")
        elif token.startswith("FAIL:"):
            result = ChargeResult(False, self._next("RF"), "模拟退款失败：通道异常")
        else:
            result = ChargeResult(True, self._next("RFD"), f"模拟退款成功 {amount_fen} 分")
        record["result"] = result.success
        self.refunds.append(record)
        return result
