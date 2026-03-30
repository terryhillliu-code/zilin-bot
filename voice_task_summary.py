#!/usr/bin/env python3
"""
语音任务每日汇总脚本

汇总当天的语音任务：
- 待办任务数量
- 今日完成任务数量
- 高优先级任务列表

输出格式：飞书消息
"""

import os
import sys
from datetime import datetime
from pathlib import Path

# 添加 zhiwei-bot 路径
zhiwei_bot_dir = Path(__file__).parent
if str(zhiwei_bot_dir) not in sys.path:
    sys.path.insert(0, str(zhiwei_bot_dir))

from voice_task_store import VoiceTaskStore, create_daily_note


def generate_summary() -> str:
    """生成每日汇总内容"""
    store = VoiceTaskStore()
    stats = store.stats()
    pending = store.list_pending(limit=10)
    done_today = store.list_done_today(limit=10)

    today = datetime.now().strftime("%Y-%m-%d")

    lines = [
        f"## 📋 每日语音任务汇总 ({today})",
        "",
        f"**统计**: 待办 {stats['pending']} | 今日完成 {stats['done_today']} | 已取消 {stats['cancelled']}",
        "",
    ]

    # 高优先级待办
    high_priority = [t for t in pending if t['priority'] == 'high']
    if high_priority:
        lines.append("### 🔴 高优先级待办")
        for task in high_priority[:5]:
            lines.append(f"- #{task['id']}: {task['content']}")
        lines.append("")

    # 普通待办
    normal_pending = [t for t in pending if t['priority'] != 'high']
    if normal_pending:
        lines.append("### 📝 待办任务")
        for task in normal_pending[:5]:
            lines.append(f"- #{task['id']}: {task['content']}")
        lines.append("")

    # 今日完成
    if done_today:
        lines.append("### ✅ 今日完成")
        for task in done_today[:5]:
            lines.append(f"- {task['content']}")
        lines.append("")

    # 提示
    if stats['pending'] > 0:
        lines.append("> 💡 发送「完成任务 #ID」标记完成")

    return "\n".join(lines)


def main():
    """主函数"""
    try:
        summary = generate_summary()
        print(summary)

        # 同时创建 Obsidian 笔记
        store = VoiceTaskStore()
        pending = store.list_pending(limit=20)
        done_today = store.list_done_today(limit=20)

        if pending or done_today:
            note_path = create_daily_note(pending, done_today)
            print(f"\n📝 已创建笔记: {note_path}")

    except Exception as e:
        print(f"❌ 汇总失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()