import os
import json
from zhiwei_agent.integrations.command_adapter import create_task_and_enqueue


def handle_research_commands(text_lower, text_stripped, user_id, message_id, ctx):
    """处理研究任务命令：映射为 ResearchTask 并入队到 TaskStore"""

    if text_lower.startswith("/notebooklm ") or text_lower.startswith("/report ") or text_lower.startswith("/research "):
        # create and enqueue task
        try:
            task_id, task = create_task_and_enqueue(text_stripped, user_id=user_id, message_id=message_id)
            ctx.reply_message(message_id, f"✅ 已创建研究任务，任务ID: {task_id}\n主题: {task.topic}\n状态: pending")
        except Exception as e:
            ctx.reply_message(message_id, f"⚠️ 创建研究任务失败: {e}")
        return True

    return False
