"""
飞书 API 调用配额管理
月限额：10,000 次
安全阈值：每天 300 次（月均 9,000 次）
"""
import os
import json
from datetime import datetime, date
from pathlib import Path

QUOTA_FILE = Path.home() / "logs" / "feishu_quota.json"
DAILY_LIMIT = 300
MONTHLY_LIMIT = 10000


def record_call(api_type: str = "reply"):
    """记录一次 API 调用"""
    data = _load()
    today = date.today().isoformat()
    month = today[:7]  # 2026-03

    if today not in data["daily"]:
        data["daily"][today] = 0
    if month not in data["monthly"]:
        data["monthly"][month] = 0

    data["daily"][today] += 1
    data["monthly"][month] += 1
    data["last_call"] = datetime.now().isoformat()

    _save(data)

    # 检查是否需要告警
    _check_alert(data["daily"][today], data["monthly"][month])


def get_stats() -> dict:
    """获取当前配额使用情况"""
    data = _load()
    today = date.today().isoformat()
    month = today[:7]
    return {
        "today": data["daily"].get(today, 0),
        "this_month": data["monthly"].get(month, 0),
        "daily_limit": DAILY_LIMIT,
        "monthly_limit": MONTHLY_LIMIT,
        "daily_remaining": DAILY_LIMIT - data["daily"].get(today, 0),
        "monthly_remaining": MONTHLY_LIMIT - data["monthly"].get(month, 0),
    }


def _check_alert(daily: int, monthly: int):
    """接近限额时推送钉钉告警"""
    if daily >= DAILY_LIMIT * 0.8:  # 日用量超过 80%
        _send_dingtalk_alert(f"⚠️ 飞书API日用量告警：今日已用 {daily}/{DAILY_LIMIT} 次")
    if monthly >= MONTHLY_LIMIT * 0.8:  # 月用量超过 80%
        _send_dingtalk_alert(f"🚨 飞书API月用量告警：本月已用 {monthly}/{MONTHLY_LIMIT} 次，请注意！")


def _send_dingtalk_alert(msg: str):
    """通过钉钉发送告警（不消耗飞书额度）"""
    try:
        sys_path = os.path.expanduser("~/zhiwei-scheduler")
        if sys_path not in __import__("sys").path:
            __import__("sys").path.insert(0, sys_path)

        from pusher import DingTalkPusher

        dt_conf = __import__("yaml").safe_load(
            open(Path.home() / "zhiwei-scheduler" / "config" / "settings.yaml")
        ).get("push", {}).get("dingtalk", {})

        if dt_conf.get("enabled"):
            pusher = DingTalkPusher(dt_conf["webhook"], dt_conf["secret"])
            pusher.send_text(msg)
            print(f"📱 钉钉告警已发送: {msg}")
    except Exception as e:
        print(f"❌ 钉钉告警失败: {e}")


def _load() -> dict:
    if QUOTA_FILE.exists():
        try:
            with open(QUOTA_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass  # 配额文件加载失败，使用默认值
    return {"daily": {}, "monthly": {}, "last_call": None}


def _save(data: dict):
    QUOTA_FILE.parent.mkdir(parents=True, exist_ok=True)
    # 只保留最近 7 天的日记录
    today = date.today()
    data["daily"] = {
        k: v for k, v in data["daily"].items()
        if (today - date.fromisoformat(k)).days <= 7
    }
    with open(QUOTA_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
