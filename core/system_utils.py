#!/usr/bin/env python3
"""
知微系统核心工具函数
"""

from datetime import datetime

def is_quiet_hours(now: datetime = None) -> bool:
    """
    检查当前时间是否在静默时段（23:00-06:30）
    """
    if now is None:
        now = datetime.now()
    hour = now.hour
    minute = now.minute

    # 23:00-06:30 为静默时段
    if hour >= 23 or hour < 6:
        return True
    if hour == 6 and minute < 30:
        return True
    return False
