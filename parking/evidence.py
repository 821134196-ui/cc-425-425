"""本地识别证据文件：为每条车道消息生成一张虚构 SVG 抓拍图。

文件保存在配置的 EVIDENCE_DIR，人工放行时必须关联其中的文件。
"""
from __future__ import annotations

from pathlib import Path

from . import config

_PLATE_COLORS = {
    "BLUE": ("#0b4ea2", "#dce9ff"),
    "GREEN": ("#0f7a52", "#d9f5ea"),
    "YELLOW": ("#a97900", "#fff3cf"),
}


def plate_svg(plate: str | None, lane_name: str, device_time: str,
              confidence: float, note: str = "") -> str:
    shown = plate if plate else "无牌车 · 临停凭证"
    bg, fg = _PLATE_COLORS.get("GREEN" if plate is None else "BLUE", _PLATE_COLORS["BLUE"])
    conf_color = "#12897a" if confidence >= 0.9 else ("#b36b00" if confidence >= 0.7 else "#a32626")
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="480" height="270" viewBox="0 0 480 270">
  <rect width="480" height="270" fill="#202428"/>
  <rect x="16" y="16" width="448" height="200" rx="8" fill="#33393f"/>
  <text x="32" y="52" fill="#9fb4bd" font-family="monospace" font-size="15">{lane_name} · 抓拍证据（虚构）</text>
  <text x="32" y="78" fill="#cfe3de" font-family="monospace" font-size="14">{device_time}</text>
  <rect x="120" y="106" width="240" height="70" rx="6" fill="{bg}"/>
  <text x="240" y="152" fill="{fg}" font-family="monospace" font-size="30"
        font-weight="bold" text-anchor="middle" letter-spacing="4">{shown}</text>
  <text x="32" y="242" fill="#9fb4bd" font-family="monospace" font-size="14">识别置信度</text>
  <rect x="120" y="230" width="300" height="12" rx="6" fill="#444b52"/>
  <rect x="120" y="230" width="{int(confidence * 300)}" height="12" rx="6" fill="{conf_color}"/>
  <text x="430" y="242" fill="{conf_color}" font-family="monospace" font-size="14" text-anchor="end">{confidence:.2f}</text>
  <text x="32" y="262" fill="#78868d" font-family="monospace" font-size="12">{note}</text>
</svg>
"""


def write_evidence(filename: str, plate: str | None, lane_name: str,
                   device_time: str, confidence: float, note: str = "",
                   directory: Path | None = None) -> Path:
    target_dir = directory or config.EVIDENCE_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / filename
    path.write_text(
        plate_svg(plate, lane_name, device_time, confidence, note),
        encoding="utf-8",
    )
    return path
