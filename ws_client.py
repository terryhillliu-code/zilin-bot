"""
知微 v2.2 - 飞书机器人 (RAG 增强版)
新增特性：
- 📚 知识库检索 (RAG)：支持 /ask 命令和智能触发
- 🧠 集成 klib 向量数据库

2026-07-30 模块拆分：
- ws_heartbeat.py         心跳监控 / 连接监控线程 / 断连告警
- bus_consumer.py         MessageBus 消费线程
- message_dedup.py        消息去重与限流状态
- memory_cache_manager.py 记忆缓存过期管理
- card_actions.py         卡片交互回调
- agent_bridge.py         OpenClaw Agent 调用（含 chat_handler 降级）
- ws_net.py               DNS 容错
本文件保留：WebSocket 连接建立、事件回调注册、消息分发、主循环与模块组装。
"""

# 加载全局密钥 (必须在最前面，在导入其他模块之前)
from zhiwei_common.secrets import load_secrets
load_secrets(silent=True)

import lark_oapi as lark
import json
import re
import subprocess
import os
import sys
import tempfile
import base64
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import signal

# 导入新模块
from task_logger import TaskLogger
from message_log import message_log  # 入站消息日志
from offline_recovery import init_offline_recovery, get_offline_recovery  # 离线消息恢复

# 导入飞书 API 模块
from feishu_api import reply_message, reply_card

# 导入媒体处理模块
from media_handler import (
    download_image, compress_image_base64, handle_image_async,
    extract_video_url, extract_article_url, is_article_url, is_video_url, summarize_url,
    handle_video_async, process_video,
    download_audio, transcribe_audio, handle_voice_task_async
)

from command_handler import handle_text_async, show_help, get_session_id, get_quick_status, check_rate_limit

# ========== 拆分模块 (2026-07-30) ==========

# 消息去重与限流状态
from message_dedup import processed_messages, user_last_request, RATE_LIMIT_SECONDS

# 记忆缓存过期管理
from memory_cache_manager import memory_cache, get_memory, cleanup_memory_cache

# 心跳监控 / 连接监控（write_heartbeat 保留导出，供外部引用）
import ws_heartbeat
from ws_heartbeat import connection_status, record_message_event, write_heartbeat

# MessageBus 消费线程
from bus_consumer import start_bus_consumer

# DNS 容错
from ws_net import dns_resolve_with_retry, check_dns_available

# ========== 全局状态 ==========

# 语音待确认
pending_voice = {}

# 图片待追问
pending_image = {}

# 对话历史（轻量级，用于 /history 命令展示）
chat_history = {}
MAX_HISTORY = 20

# 线程池（根据 CPU 核心数动态设置，建议 3-5）
_max_workers = min(5, (os.cpu_count() or 2))
executor = ThreadPoolExecutor(max_workers=_max_workers, thread_name_prefix="msg_handler")

# 审批待确认 (T-056)
pending_review = {}  # user_id -> task_id

# 视频重复确认
pending_video_confirm = {}  # user_id -> {url, history, text, message_id}

# 最近活跃用户 (T-056) — 持久化到文件
FEISHU_USER_FILE = os.path.expanduser("~/tasks/.feishu_user_id")
last_active_user = {"user_id": None}


def save_active_user(user_id: str):
    """持久化飞书用户 ID"""
    last_active_user["user_id"] = user_id
    try:
        with open(FEISHU_USER_FILE, "w") as f:
            f.write(user_id)
    except Exception:
        pass  # 用户ID持久化失败不影响主流程


def load_active_user() -> str:
    """从文件加载飞书用户 ID"""
    try:
        if os.path.exists(FEISHU_USER_FILE):
            with open(FEISHU_USER_FILE) as f:
                uid = f.read().strip()
                if uid:
                    last_active_user["user_id"] = uid
                    return uid
    except Exception:
        pass  # 用户ID加载失败不影响主流程
    return None


# ========== RAG 知识库功能 (Phase 4 新增) ==========

def query_knowledge_base(query: str, top_k: int = 3) -> str:
    """检索知识库 - 委托给 rag_bridge 模块"""
    from rag_bridge import get_context
    print(f"📚 RAG 检索：{query}")
    return get_context(query, top_k=top_k) or None




# ========== 应用配置 ==========

APP_ID = os.environ.get("FEISHU_APP_ID")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET")

if not APP_ID or not APP_SECRET:
    print("❌ 错误：未能在环境变量中找到 FEISHU_APP_ID/SECRET。请检查 .env 文件。")
    sys.exit(1)

client = lark.Client.builder() \
    .app_id(APP_ID) \
    .app_secret(APP_SECRET) \
    .build()

# 初始化飞书 API 模块
from feishu_api import init_feishu_api
init_feishu_api(client)

# 初始化媒体处理模块
from media_handler import init_media_handler, init_media_handler_with_audio
init_media_handler(client, reply_message, TaskLogger, pending_image, pending_voice, time)

# 初始化 TTS 语音回复依赖
from feishu_api import send_audio_reply as _send_audio_reply
init_media_handler_with_audio(_send_audio_reply)


def cleanup_pending_images():
    """清理过期的待处理图片（10 分钟过期）"""
    current_time = time.time()
    expired = []
    for user_id, data in pending_image.items():
        if isinstance(data, dict):
            if current_time - data.get("time", 0) > 600:
                expired.append(user_id)
        else:
            expired.append(user_id)
    for user_id in expired:
        del pending_image[user_id]
    # 同时清理 memory_cache
    cleanup_memory_cache()


def add_to_history(user_id: str, role: str, text: str):
    if user_id not in chat_history:
        chat_history[user_id] = deque(maxlen=MAX_HISTORY)
    chat_history[user_id].append((time.strftime("%H:%M"), role, text[:100]))


def get_history(user_id: str) -> str:
    if user_id not in chat_history or not chat_history[user_id]:
        return "📜 暂无对话记录"
    lines = ["📜 最近对话记录\n"]
    for t, role, text in chat_history[user_id]:
        icon = "👤" if role == "user" else "🤖"
        lines.append(f"{t} {icon} {text}")
    return "\n".join(lines)


# V2-203 / v55.6: ChatHandler 与 OpenClaw 调用已拆分至 agent_bridge.py
from agent_bridge import get_chat_handler, call_openclaw_agent

# 初始化命令处理模块（需要 call_openclaw_agent 已定义）
from command_handler import init_command_handler
init_command_handler(
    reply_message, reply_card, call_openclaw_agent, query_knowledge_base,
    get_memory, add_to_history, get_history,
    is_article_url, is_video_url, summarize_url, handle_video_async,
    extract_video_url, extract_article_url, TaskLogger,
    save_active_user, load_active_user,
    chat_history, pending_voice, pending_image, pending_review,
    MAX_HISTORY, RATE_LIMIT_SECONDS, user_last_request, memory_cache,
    get_chat_handler,  # V2-203: 新增 chat_handler
    global_pending_video_confirm=pending_video_confirm  # 视频重复确认
)

# ========== 消息分发 ==========

def _trigger_self_heal(cmd: str, message_id: str, chat_id_hint: str = None):
    """收到 /heal 或 /checkup 时异步触发个人 Mac 自检。"""
    import subprocess
    flag = "--full" if cmd == "/checkup" else "--quick"
    push_flag = ["--push"]
    if chat_id_hint:
        push_flag += [f"--target={chat_id_hint}"]
    try:
        reply_message(message_id, f"🩺 已触发 Mac 自检 ({cmd})，结果稍后推送")
    except Exception:
        pass
    try:
        r = subprocess.run(
            ["/opt/homebrew/bin/python3.14", "/Users/liufang/mac-self-heal/cli.py",
             "daemon-tick", flag] + push_flag,
            capture_output=True, text=True, timeout=240,
        )
        print(f"[self_heal] {cmd} rc={r.returncode}\n{(r.stdout or '')[-400:]}")
    except Exception as e:
        print(f"[self_heal] {cmd} 调用失败: {e!r}")


def do_p2_im_message_receive_v1(data) -> None:
    # 递增消息事件计数器（2026-06-02 加固，状态在 ws_heartbeat 模块）
    record_message_event()

    # 获取消息 ID 和类型
    message_id = "N/A"
    msg_type = "unknown"
    try:
        message_id = data.event.message.message_id
        msg_type = data.event.message.message_type
    except AttributeError:
        pass

    print(f"📡 [Event] 收到消息：type={msg_type}, id={message_id}")

    # 更新最后事件时间以监控连接状态
    connection_status["last_event"] = time.time()
    connection_status["connected"] = True

    # ⭐ 入站消息日志（在去重之前记录，确保所有消息都被记录）
    try:
        # 提取用户 ID 用于日志
        sender = data.event.sender if hasattr(data, 'event') else None
        log_user_id = "unknown"
        if sender and hasattr(sender, 'sender_id') and sender.sender_id:
            log_user_id = sender.sender_id.open_id or sender.sender_id.user_id or "unknown"

        # 提取内容摘要
        log_content = None
        try:
            if hasattr(data.event, 'message') and data.event.message.content:
                import json
                content_dict = json.loads(data.event.message.content)
                if msg_type == "text":
                    log_content = content_dict.get("text", "")[:500]
                elif msg_type == "image":
                    log_content = f"image_key: {content_dict.get('image_key', '')}"
                elif msg_type == "audio":
                    log_content = f"file_key: {content_dict.get('file_key', '')}"
                else:
                    log_content = str(content_dict)[:500]
        except (json.JSONDecodeError, AttributeError, KeyError):
            pass  # 消息内容解析失败，使用 None

        message_log.log(message_id, log_user_id, msg_type, log_content)
    except Exception as e:
        print(f"⚠️ 消息日志记录异常: {e}")

    try:
        event = data.event
        message = event.message

        cleanup_pending_images()

        # 去重
        if message_id in processed_messages:
            print(f"⏭️ 消息 {message_id} 已处理过，跳过")
            return
        processed_messages.append(message_id)

        # 限流 & 识别用户
        sender = event.sender
        temp_user_id = "unknown"
        if sender and sender.sender_id:
            # 优先使用 open_id，因为它在 API 调用中更通用且需要权限较少
            temp_user_id = sender.sender_id.open_id or sender.sender_id.user_id or "unknown"
        
        # 保存最近活跃用户
        if temp_user_id != "unknown":
            save_active_user(temp_user_id)

            # ⭐ 同时缓存 chat_id 用于离线恢复
            if hasattr(message, 'chat_id') and message.chat_id:
                offline_recovery = get_offline_recovery()
                if offline_recovery:
                    alt_uid = sender.sender_id.user_id if sender and sender.sender_id else None
                    offline_recovery.cache_chat_id(temp_user_id, message.chat_id, alt_user_id=alt_uid)

        if not check_rate_limit(temp_user_id):
            print(f"⚠️ 限流：{temp_user_id}")
            return

        msg_type = message.message_type
        content_str = message.content

        user_id = "unknown"
        if sender and sender.sender_id:
            user_id = sender.sender_id.user_id or sender.sender_id.open_id or sender.sender_id.union_id or "unknown"

        print(f"\n{'=' * 50}")
        print(f"📨 [{msg_type}] 用户：{str(user_id)[:10]}...")

        # T-056: 不再二次保存 user_id 格式（与 open_id 格式的 cache key 冲突）

        content_dict = json.loads(content_str)

        if msg_type == "text":
            text = content_dict.get("text", "")
            text = re.sub(r'@_user_\d+\s*', '', text).strip()
            print(f"   文本：{text[:50]}...")
            if text in ("/heal", "/checkup"):
                _chat_id = getattr(message, "chat_id", None)
                executor.submit(_trigger_self_heal, text, message_id, _chat_id)
                return
            if text:
                executor.submit(handle_text_async, text, user_id, message_id)

        elif msg_type == "audio":
            file_key = content_dict.get("file_key", "")
            print(f"   语音：{file_key[:30]}...")
            reply_message(message_id, "🎤 正在识别语音...")
            executor.submit(handle_voice_task_async, message_id, file_key, user_id)

        elif msg_type == "image":
            image_key = content_dict.get("image_key", "")
            print(f"   图片：{image_key[:30]}...")
            reply_message(message_id, "🖼️ 正在分析图片，请稍候...")
            executor.submit(handle_image_async, message_id, image_key, user_id)

        elif msg_type in ["media", "file"]:
            reply_message(message_id,
                "📁 暂不支持该文件类型\n\n支持：文字 | 图片 | 网页链接 | 视频链接")

        else:
            print(f"   ⏭️ 不支持：{msg_type}")

        print(f"{'=' * 50}")

    except Exception as e:
        print(f"❌ 处理错误：{e}")
        import traceback
        traceback.print_exc()


# 卡片交互回调（拆分至 card_actions.py）
from card_actions import do_p2_card_action_trigger_v1


def main():
    import time  # 模块级 time 在嵌套函数闭包中可能不可用 (Python 3.14)

    # ⭐ DNS 预热：启动前尝试解析飞书域名，加速首次连接
    print("🔍 DNS 预热检查...")
    dns_resolve_with_retry("open.feishu.cn")
    dns_resolve_with_retry("msg-frontier.feishu.cn")

    event_handler = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(do_p2_im_message_receive_v1) \
        .register_p2_card_action_trigger(do_p2_card_action_trigger_v1) \
        .build()

    print(f"🔧 启动 WebSocket 客户端 (AppID: {APP_ID})")
    cli = lark.ws.Client(
        APP_ID,
        APP_SECRET,
        event_handler=event_handler,
        log_level=lark.LogLevel.INFO
    )

    # 信号处理，实现优雅退出 (ISSUE-027)
    def handle_exit(sig, frame):
        print(f"\n🛑 收到信号 {sig}，正在优雅退出...")
        try:
            executor.shutdown(wait=False)
        except: pass
        try:
            cli.stop()
        except: pass
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    # WebSocket 配置优化（VPN 环境容错）
    # 原配置 (ISSUE-003): ping=10s, reconnect=8s → 对 VPN 太激进，频繁断连
    # 优化后: ping=60s, reconnect=30s → 容忍 VPN 30s 内的网络抖动
    _original_configure = cli._configure

    _configure_logged = [False]
    def _patched_configure(conf):
        _original_configure(conf)
        cli._ping_interval = 60
        cli._reconnect_interval = 30
        cli._reconnect_nonce = 15
        cli._reconnect_count = -1
        if not _configure_logged[0]:
            print(f"🔄 WebSocket 配置已锁定：ping={cli._ping_interval}s, 重连={cli._reconnect_interval}s, 重试=无限")
            _configure_logged[0] = True
    cli._configure = _patched_configure

    print("🤖 知微 v2.1 启动 (RAG 增强版)")
    print("   新增：知识库检索 (/ask 或 '查一下')")
    print("   特性：三层记忆 | 意图路由 | 任务日志")
    print("   支持：文字 | 图片 | 网页链接 | 视频链接")
    print("-" * 50)

    # ⭐ 初始化离线恢复模块（直接从环境变量获取 bot_id）
    try:
        bot_id = os.getenv("FEISHU_APP_ID")
        if bot_id:
            init_offline_recovery(client, bot_id)
            print(f"✅ 离线恢复模块已初始化 (bot_id: {bot_id[:8]}...)")
        else:
            print("⚠️ 未找到 FEISHU_APP_ID 环境变量，离线恢复模块未启用")
    except Exception as e:
        print(f"⚠️ 离线恢复模块初始化失败: {e}")

    # ⭐ 初始化视频处理告警用户
    try:
        from video_history import set_alert_user
        alert_user_id = os.getenv("ALERT_USER_ID")
        if alert_user_id:
            set_alert_user(alert_user_id)
            print(f"✅ 视频处理告警用户已设置: {alert_user_id[:8]}...")
        else:
            print("ℹ️ ALERT_USER_ID 未配置，视频处理告警未启用")
    except Exception as e:
        print(f"⚠️ 告警用户设置失败: {e}")

    # ISSUE-003: 断连监控和告警线程（拆分至 ws_heartbeat.py，依赖注入避免循环 import）
    ws_heartbeat.start_connection_monitor(
        get_offline_recovery=get_offline_recovery,
        load_active_user=load_active_user,
        cleanup_memory_cache=cleanup_memory_cache,
    )

    # 启动统一的 MessageBus 消费线程（拆分至 bus_consumer.py，依赖注入避免循环 import）
    start_bus_consumer(client, APP_ID, last_active_user, load_active_user)

    try:
        cli.start()
    except KeyboardInterrupt:
        print("👋 收到中断信号，正常退出")
    except Exception as e:
        print(f"❌ 主程序异常退出: {e}")
        time.sleep(10)  # 退出前等待 10 秒，防止快速重启循环
        raise


if __name__ == "__main__":
    main()
