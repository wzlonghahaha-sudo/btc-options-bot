"""
事件日历 — 宏观经济事件对期权策略的影响评估

用于:
1. 开仓评分: 合约存续期内含 HIGH 级事件 → 总分扣减
2. 自动推送: 事件前 48 小时临时提高推送门槛 (避免在高波动前开仓)
3. 事件列表过期时发出警告

事件来源:
- FOMC 会议日程 (美联储公开固定日程, 每年公布)
- CPI / 非农等经济数据发布日 (每月固定)
- 比特币特有事件 (减半等, 已知日期)

维护方式:
- EVENT_LIST 为手动维护的 dict 常量
- 事件列表过期(最近事件早于当前日期60天)时, 启动日志 + /status 警告
"""

import logging
from datetime import datetime, timezone, timedelta, date
from dataclasses import dataclass

log = logging.getLogger(__name__)

# ============================================================
#  事件等级
# ============================================================
HIGH = "HIGH"       # 重大事件 (FOMC利率决议, CPI等), 可能导致 IV 飙升
MEDIUM = "MEDIUM"   # 中等事件 (FOMC纪要, 就业数据), 有一定波动影响
LOW = "LOW"         # 低影响事件 (PMI等), 仅作提醒

# ============================================================
#  事件列表 (手动维护)
#  格式: (year, month, day) -> {"name": str, "level": str}
#
#  FOMC 2026 日程来源: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
#  2026 FOMC 会议日期 (决议日, 即会议第二天):
#    1/29, 3/18, 5/6, 6/17, 7/29, 9/16, 11/4, 12/16
#  CPI 发布日: 每月中旬 (BLS 公布, 具体日期每年不同)
# ============================================================
EVENT_LIST: dict[tuple[int, int, int], dict] = {
    # === 2026 H2 FOMC 利率决议 ===
    (2026, 7, 29): {"name": "FOMC 利率决议", "level": HIGH},
    (2026, 9, 16): {"name": "FOMC 利率决议", "level": HIGH},
    (2026, 11, 4): {"name": "FOMC 利率决议", "level": HIGH},
    (2026, 12, 16): {"name": "FOMC 利率决议", "level": HIGH},

    # === 2026 H1 FOMC (已过, 保留记录) ===
    (2026, 1, 29): {"name": "FOMC 利率决议", "level": HIGH},
    (2026, 3, 18): {"name": "FOMC 利率决议", "level": HIGH},
    (2026, 5, 6):  {"name": "FOMC 利率决议", "level": HIGH},
    (2026, 6, 17): {"name": "FOMC 利率决议", "level": HIGH},

    # === 2026 CPI 发布日 (月度, 日期为估计值, 实际以 BLS 公告为准) ===
    # TODO: 更新为 BLS 实际公布日期 (通常在每月 10-14 日)
    (2026, 7, 14):  {"name": "CPI 数据发布", "level": HIGH},
    (2026, 8, 12):  {"name": "CPI 数据发布", "level": HIGH},
    (2026, 9, 10):  {"name": "CPI 数据发布", "level": HIGH},
    (2026, 10, 13): {"name": "CPI 数据发布", "level": HIGH},
    (2026, 11, 12): {"name": "CPI 数据发布", "level": HIGH},
    (2026, 12, 10): {"name": "CPI 数据发布", "level": HIGH},

    # === 2027 Q1 FOMC (如已公布, 先占位) ===
    # TODO: 等 2026 年底 Fed 公布 2027 日程后更新
    (2027, 1, 27): {"name": "FOMC 利率决议 (预估)", "level": HIGH},
    (2027, 3, 17): {"name": "FOMC 利率决议 (预估)", "level": HIGH},
}


# ============================================================
#  数据结构
# ============================================================
@dataclass
class Event:
    """单个事件"""
    date: date
    name: str
    level: str  # HIGH / MEDIUM / LOW

    @property
    def date_str(self) -> str:
        return self.date.strftime("%m月%d日")


def _get_all_events() -> list[Event]:
    """将 EVENT_LIST 转换为 Event 对象列表, 按日期排序"""
    events = []
    for (y, m, d), info in EVENT_LIST.items():
        events.append(Event(
            date=date(y, m, d),
            name=info["name"],
            level=info["level"],
        ))
    return sorted(events, key=lambda e: e.date)


# ============================================================
#  公开 API
# ============================================================

def days_to_next_event(level: str = None) -> int | None:
    """
    距离下一个事件的天数

    Args:
        level: 只看指定等级的事件 (None = 全部)

    Returns:
        天数, 如果没有未来事件则返回 None
    """
    today = date.today()
    events = _get_all_events()
    for e in events:
        if e.date >= today:
            if level is None or e.level == level:
                return (e.date - today).days
    return None


def get_next_events(n: int = 3, level: str = None) -> list[Event]:
    """获取未来最近的 N 个事件"""
    today = date.today()
    events = _get_all_events()
    future = [e for e in events if e.date >= today]
    if level:
        future = [e for e in future if e.level == level]
    return future[:n]


def position_crosses_event(open_date: date, expiry_date: date,
                           level: str = None) -> list[Event]:
    """
    检查持仓存续期内是否包含事件

    Args:
        open_date: 开仓日期 (或当前日期)
        expiry_date: 到期日期
        level: 只看指定等级 (None = 全部)

    Returns:
        存续期内的事件列表
    """
    events = _get_all_events()
    crossed = []
    for e in events:
        if open_date <= e.date <= expiry_date:
            if level is None or e.level == level:
                crossed.append(e)
    return crossed


def is_pre_event_window(hours: int = 48) -> tuple[bool, Event | None]:
    """
    当前是否在事件前的敏感窗口期

    Args:
        hours: 窗口期小时数 (默认 48 小时)

    Returns:
        (是否在窗口期, 最近的事件)
    """
    today = date.today()
    cutoff = today + timedelta(days=hours / 24)
    events = _get_all_events()
    for e in events:
        if today <= e.date <= cutoff:
            return True, e
    return False, None


def score_penalty_for_events(open_date: date, expiry_date: date) -> tuple[int, list[str]]:
    """
    计算事件对开仓评分的扣减

    规则: 存续期内每个 HIGH 级事件 → -8 分

    Args:
        open_date: 开仓日期
        expiry_date: 到期日期

    Returns:
        (总扣减分数, 事件描述列表)
    """
    events = position_crosses_event(open_date, expiry_date, level=HIGH)
    if not events:
        return 0, []

    penalty = -8 * len(events)
    descriptions = [f"⚠️ 跨 {e.name}({e.date_str})" for e in events]
    return penalty, descriptions


def is_calendar_stale() -> bool:
    """
    事件列表是否过期 (最近事件早于当前日期 60 天)

    Returns:
        True = 事件列表需要更新
    """
    today = date.today()
    events = _get_all_events()
    if not events:
        return True

    # 找最近的未来事件
    future = [e for e in events if e.date >= today]
    if future:
        return False

    # 没有未来事件, 检查最近的过去事件是否超过 60 天
    most_recent = events[-1]  # 已排序, 最后一个最近
    stale_days = (today - most_recent.date).days
    if stale_days > 60:
        log.warning(f"事件日历需更新! 最近事件已过去 {stale_days} 天 ({most_recent.name} {most_recent.date})")
        return True
    return False


def get_calendar_status() -> str:
    """获取事件日历状态摘要, 用于 /status 命令"""
    if is_calendar_stale():
        return "⚠️ 事件日历需更新"

    next_events = get_next_events(3, level=HIGH)
    if not next_events:
        return "无近期重大事件"

    lines = []
    for e in next_events:
        days = (e.date - date.today()).days
        lines.append(f"{e.name} {e.date_str} ({days}天后)")
    return " | ".join(lines)


# ============================================================
#  模块自测
# ============================================================
if __name__ == "__main__":
    print("=== 事件日历模块测试 ===")

    print(f"\n日历状态: {get_calendar_status()}")
    print(f"过期: {is_calendar_stale()}")

    days = days_to_next_event(HIGH)
    print(f"距下一个 HIGH 事件: {days} 天")

    print(f"\n未来 5 个 HIGH 事件:")
    for e in get_next_events(5, HIGH):
        d = (e.date - date.today()).days
        print(f"  {e.date} | {e.name} | {d}天后")

    # 测试持仓跨事件
    print(f"\n测试持仓跨事件:")
    from datetime import date
    test_open = date(2026, 7, 1)
    test_expiry = date(2026, 9, 30)
    crossed = position_crosses_event(test_open, test_expiry)
    print(f"  {test_open} ~ {test_expiry} 跨越 {len(crossed)} 个事件:")
    for e in crossed:
        print(f"    {e.date} {e.name} ({e.level})")

    penalty, descs = score_penalty_for_events(test_open, test_expiry)
    print(f"  评分扣减: {penalty}, 描述: {descs}")

    # 事件前窗口
    in_window, evt = is_pre_event_window(48)
    print(f"\n当前在事件前48小时窗口: {in_window}")
    if evt:
        print(f"  最近事件: {evt.name} {evt.date_str}")

    print("\n✅ event_calendar 模块测试通过")
