import os
import time
import json
import traceback
import sys
import subprocess
from pathlib import Path
from types import SimpleNamespace

# 导入共享库
from zhiwei_common.config import ZHIWEI_DEV
from zhiwei_common.task_client import TaskStore

# 导入子命令模块
from commands import (
    handle_dev_commands,
    handle_research_commands,
    handle_system_commands,
    handle_knowledge_commands,
    handle_media_commands,
    handle_agent_commands,
    handle_lark_commands,  # ⭐ v57.0
    ChatHandler
)

# 全局上下文容器
_ctx = SimpleNamespace()

def init_command_handler(*args, **kwargs):
    """
    初始化指令处理器，接收来自 ws_client.py 的所有依赖注入
    兼容旧版本的多参数调用与关键字调用
    """
    global _ctx
    # 按照 ws_client.py 的顺序进行映射
    arg_names = [
        "reply_message", "reply_card", "call_openclaw_agent", "query_knowledge_base",
        "get_memory", "add_to_history", "get_history",
        "is_article_url", "is_video_url", "summarize_url", "handle_video_async",
        "extract_video_url", "extract_article_url", "TaskLogger",
        "IntentRouter", "save_active_user", "load_active_user",
        "chat_history", "pending_voice", "pending_image", "pending_review",
        "MAX_HISTORY", "RATE_LIMIT_SECONDS", "user_last_request", "memory_cache",
        "pending_video_confirm", "get_video_history", "get_chat_handler"
    ]
    
    for i, val in enumerate(args):
        if i < len(arg_names):
            setattr(_ctx, arg_names[i], val)
            
    for k, v in kwargs.items():
        setattr(_ctx, k, v)
        
    print("✅ CommandHandler (Router) 初始化完成")

def handle_text_async(text, user_id, message_id, user_role="user"):
    """
    WebSocket 异步文本处理入口
    """
    text_stripped = text.strip()
    text_lower = text_stripped.lower()
    session_id = f"feishu-{user_id}"

    try:
        # 1. 基础命令拦截
        if handle_dev_commands(text_lower, text_stripped, user_id, message_id, _ctx):
            return

        if handle_research_commands(text_lower, text_stripped, user_id, message_id, _ctx):
            return

        if handle_knowledge_commands(text_lower, text_stripped, user_id, message_id, _ctx):
            return

        if handle_system_commands(text_lower, text_stripped, user_id, message_id, _ctx):
            return

        if handle_media_commands(text_lower, text_stripped, user_id, message_id, _ctx):
            return

        # ⭐ v57.0 飞书操作命令
        if handle_lark_commands(text_lower, text_stripped, user_id, message_id, _ctx):
            return

        # 2. Agent 智能路由 (Layer 2/3)
        if handle_agent_commands(text_lower, text_stripped, user_id, message_id, _ctx):
            return

        # 3. 传统对话
        handler = ChatHandler(_ctx.reply_message, _ctx.reply_card, _ctx.get_memory, _ctx.add_to_history)
        handler.handle_chat_message(text_stripped, user_id, message_id, session_id)

    except Exception as e:
        print(f"❌ 文本处理异常: {e}")
        traceback.print_exc()
        _ctx.reply_message(message_id, f"❌ 处理异常，请重试")

# 兼容函数
def show_help():
    return "💡 请发送 /help 查看详细指令帮助"

def get_session_id(user_id):
    return f"feishu-{user_id}"

def get_quick_status():
    from core.health_check import get_system_health_dict, format_health_status
    return format_health_status(get_system_health_dict())

def check_rate_limit(user_id):
    now = time.time()
    last = _ctx.user_last_request.get(user_id, 0)
    if now - last < _ctx.RATE_LIMIT_SECONDS:
        return False
    _ctx.user_last_request[user_id] = now
    return True
