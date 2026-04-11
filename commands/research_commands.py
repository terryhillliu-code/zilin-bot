import os
import json
from zhiwei_common.task_client import TaskStore

def handle_research_commands(text_lower, text_stripped, user_id, message_id, ctx):
    """处理研究任务命令 - 已移交探微"""

    if text_lower.startswith("/notebooklm ") or text_lower.startswith("/report ") or text_lower.startswith("/research "):
        ctx.reply_message(message_id,
            "📌 研究任务已移交「探微」机器人\n\n"
            "请 @探微 或私聊探微发送：\n"
            "`/research <主题>` - 深度研究\n"
            "`/notebooklm <主题>` - 研究笔记\n\n"
            "💡 知微专注实时交互：对话、视频、图片、知识库"
        )
        return True

    return False
