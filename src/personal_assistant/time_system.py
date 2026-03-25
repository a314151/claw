from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


TIME_QUERY_KEYWORDS: list[str] = [
    "现在几点",
    "现在时间",
    "当前时间",
    "北京时间",
    "几点了",
    "今天几号",
    "星期几",
    "time",
    "date",
    "what time",
]


@dataclass(slots=True)
class TimePolicy:
    timezone: str = "Asia/Shanghai"
    display_format: str = "%Y年%m月%d日 %H:%M:%S"


class TimeSystem:
    """统一时钟源、时区策略、显示格式与时间快照。"""

    def __init__(self, policy: TimePolicy | None = None) -> None:
        self._policy = policy or TimePolicy(
            timezone=(os.getenv("APP_TIMEZONE", "Asia/Shanghai").strip() or "Asia/Shanghai"),
            display_format=(os.getenv("APP_TIME_FORMAT", "%Y年%m月%d日 %H:%M:%S").strip() or "%Y年%m月%d日 %H:%M:%S"),
        )

    @property
    def policy(self) -> TimePolicy:
        return self._policy

    def now(self) -> datetime:
        try:
            return datetime.now(ZoneInfo(self._policy.timezone))
        except ZoneInfoNotFoundError:
            return datetime.now()

    def timezone_label(self) -> str:
        try:
            ZoneInfo(self._policy.timezone)
            return self._policy.timezone
        except ZoneInfoNotFoundError:
            return "系统本地时区"

    def now_ts(self) -> int:
        return int(self.now().timestamp())

    def format_ts(self, ts: int | float | None = None) -> str:
        if ts is None:
            dt = self.now()
        else:
            dt = self.normalize_to_datetime(ts)
        return dt.strftime(self._policy.display_format)

    def normalize_to_datetime(self, ts: int | float) -> datetime:
        v = float(ts)
        if v > 1e12:
            v /= 1000.0
        try:
            return datetime.fromtimestamp(v, ZoneInfo(self._policy.timezone))
        except ZoneInfoNotFoundError:
            return datetime.fromtimestamp(v)

    def build_time_memory_node(self) -> dict[str, Any]:
        dt = self.now()
        week_map = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
        return {
            "clock_source": "server_local_clock",
            "timezone": self.timezone_label(),
            "unix_ts": int(dt.timestamp()),
            "iso": dt.isoformat(),
            "date": dt.strftime("%Y-%m-%d"),
            "time": dt.strftime("%H:%M:%S"),
            "weekday": week_map[dt.weekday()],
        }


def is_time_query(message: str) -> bool:
    text = (message or "").strip().lower()
    if not text:
        return False
    return any(k in text for k in TIME_QUERY_KEYWORDS)


def build_local_time_reply(time_system: TimeSystem) -> str:
    node = time_system.build_time_memory_node()
    return (
        f"根据服务器本地时钟（{node['timezone']}）读取到的当前时间：\n\n"
        f"{node['date']} {node['weekday']} {node['time']}\n\n"
        "如果你电脑右下角时间与此不一致，优先以你本机系统时间为准。"
    )


def normalize_keyword_list(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for item in values or []:
        t = re.sub(r"\s+", " ", str(item or "").strip().lower())
        if t:
            out.append(t)
    return out
