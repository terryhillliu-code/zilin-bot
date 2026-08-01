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
    handle_podcast_commands,  # ⭐ 播客管理命令
    handle_youtube_commands,  # ⭐ v71 YouTube 频道追更
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
        "save_active_user", "load_active_user",
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

    # ⭐ 2026-07-26 P2 语音接线：语音转写确认消费（此前 pending_voice 只写不读）
    _pending_voice = getattr(_ctx, 'pending_voice', None)
    if _pending_voice is not None and user_id in _pending_voice:
        _confirm = ('确认', '确定', '提取', '是', '好', '好的', 'ok', 'yes')
        _cancel = ('取消', '不', '不要', '算了', 'no')
        if text_lower in _confirm:
            _voice_text = _pending_voice.pop(user_id, {}).get('text', '')
            if _voice_text:
                _ctx.reply_message(message_id, f"🎙️ 正在处理语音内容：{_voice_text[:60]}…")
                return handle_text_async(_voice_text, user_id, message_id, user_role)
        elif text_lower in _cancel:
            _pending_voice.pop(user_id, None)
            _ctx.reply_message(message_id, "已取消，语音内容已丢弃。")
            return

    # ⭐ F2 2026-07-31 图片追问：pending_image 已存 base64（10 分钟有效）但从未被消费，
    # 之前只能单次分析。现支持发图后紧接追问（/vision <问题> 或直接提问），
    # 多轮追问复用同一张图，走 zhiwei_common.llm.call_vision 统一出口。
    _pending_image = getattr(_ctx, 'pending_image', None)
    if _pending_image is not None and user_id in _pending_image:
        _img = _pending_image.get(user_id) or {}
        _b64 = _img.get("base64") if isinstance(_img, dict) else None
        _is_vision_cmd = text_lower.startswith("/vision")
        _question = text_stripped[len("/vision"):].strip() if _is_vision_cmd else text_stripped
        # 命令式总是追问；非命令则仅当不是其他斜杠命令/空消息时视为追问
        if _b64 and _question and (_is_vision_cmd or not text_stripped.startswith("/")):
            try:
                from zhiwei_common.llm import call_vision
                _ctx.reply_message(message_id, f"🖼️ 就刚才那张图追问：{_question[:40]}…")
                ok, ans = call_vision(_question, image_b64=_b64, max_tokens=2000)
                # 刷新时间戳，让多轮追问不致于 10 分钟窗口中途过期
                if isinstance(_img, dict):
                    _img["time"] = time.time()
                _ctx.reply_message(
                    message_id,
                    f"🖼️ **图片追问**\n\n{ans}" if ok else f"❌ 图片追问失败: {ans}")
                return
            except Exception as e:
                print(f"⚠️ 图片追问异常，降级走常规流程: {e}")

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

        # ⭐ 播客管理命令
        if handle_podcast_commands(text_lower, text_stripped, user_id, message_id, _ctx):
            return

        # ⭐ v71 YouTube 频道追更命令
        if handle_youtube_commands(text_lower, text_stripped, user_id, message_id, _ctx):
            return

        # ⭐ v57.0 飞书操作命令
        if handle_lark_commands(text_lower, text_stripped, user_id, message_id, _ctx):
            return

        # 2. Agent 智能路由 (Layer 2/3)
        if handle_agent_commands(text_lower, text_stripped, user_id, message_id, _ctx):
            return

        # 2.5 自然语言主路由（2026-07-26 P1）：斜杠命令全部落空后、对话兜底前
        from commands.nl_router import route_natural_language
        if route_natural_language(text_stripped, user_id, message_id, _ctx):
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
