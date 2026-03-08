from openclaw_api import OpenClawClient
"""
知微 v2.1 - OpenClaw Agent 飞书机器人 (RAG 增强版)
新增特性：
- 📚 知识库检索 (RAG)：支持 /ask 命令和智能触发
- 🧠 集成 klib 向量数据库
"""
import lark_oapi as lark
from lark_oapi.api.im.v1 import *
import json
import re
import subprocess
import os
import tempfile
import base64
import threading
import time
from pathlib import Path
from collections import defaultdict, deque

# 导入新模块
from memory_manager import MemoryManager
from task_logger import TaskLogger
from agent_chain import detect_chain_intent, execute_chain
from intent_router import IntentRouter

# 导入飞书 API 模块
from feishu_api import reply_message, reply_card

# 导入媒体处理模块
from media_handler import (
    download_image, compress_image_base64, handle_image_async,
    extract_video_url, extract_article_url, is_article_url, is_video_url, summarize_url,
    handle_video_async, process_video,
    download_audio, transcribe_audio
)

from command_handler import handle_text_async, show_help, get_session_id, get_quick_status, check_rate_limit

import sys
import os
sys.path.insert(0, os.path.expanduser("~/zhiwei-dev"))
from message_bus import MessageBus

# ========== 全局状态 ==========

# 限流
user_last_request = defaultdict(float)
RATE_LIMIT_SECONDS = 2

# 语音待确认
pending_voice = {}

# 图片待追问
pending_image = {}

# 对话历史（轻量级，用于 /history 命令展示）
chat_history = {}
MAX_HISTORY = 20

# 记忆管理器缓存（user_id -> MemoryManager）
memory_cache = {}

# 消息去重
processed_messages = set()

# 连接状态监控 (ISSUE-003)
connection_status = {"connected": True, "last_event": time.time()}

# 审批待确认 (T-056)
pending_review = {}  # user_id -> task_id

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
        pass


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
        pass
    return None


# ========== RAG 知识库功能 (Phase 4 新增) ==========

def query_knowledge_base(query: str) -> str:
    """调用本地知识库 (klib) 进行检索"""
    try:
        print(f"📚 RAG 检索: {query}")
        cmd = [
            "python3",
            os.path.expanduser("~/Documents/Library/klib_query.py"),
            "search",
            query
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

        if result.returncode == 0:
            output = result.stdout.strip()
            if "No relevant results found" in output or not output:
                return None
            return output
        else:
            print(f"❌ RAG 检索失败: {result.stderr[:200]}")
            return None

    except subprocess.TimeoutExpired:
        print("❌ RAG 检索超时")
        return None
    except Exception as e:
        print(f"❌ RAG 调用异常: {e}")
        return None

# ========== 应用配置 ==========

# ========== 应用配置 ==========

# ========== 应用配置 ==========

APP_ID = os.environ.get("FEISHU_APP_ID", "cli_a9142bd071bd1bd9")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "mlIZdNRvxpaVQIB6VQxHIee6WgW4UcPf")

# 兜底：如果被错误清空，再次写死兜底
if not APP_ID or not APP_SECRET:
    APP_ID = "cli_a9142bd071bd1bd9"
    APP_SECRET = "mlIZdNRvxpaVQIB6VQxHIee6WgW4UcPf"

if not APP_ID or not APP_SECRET:
    print("❌ 错误: 未能在环境变量或 settings.yaml 中找到 FEISHU_APP_ID/SECRET")

client = lark.Client.builder() \
    .app_id(APP_ID) \
    .app_secret(APP_SECRET) \
    .build()

# 初始化飞书 API 模块
from feishu_api import init_feishu_api
init_feishu_api(client)

# 初始化媒体处理模块
from media_handler import init_media_handler
init_media_handler(client, reply_message, TaskLogger, pending_image, time)


def get_memory(user_id: str) -> MemoryManager:
    """获取或创建用户的记忆管理器"""
    if user_id not in memory_cache:
        memory_cache[user_id] = MemoryManager(user_id)
    return memory_cache[user_id]


def cleanup_pending_images():
    """清理过期的待处理图片（10分钟过期）"""
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


def call_openclaw_agent(message: str, session_id: str, agent: str = "main") -> str:
    """调用 OpenClaw Agent"""
    try:
        cmd = [
            "/usr/local/bin/docker", "exec", "clawdbot",
            "openclaw", "agent",
            "--agent", agent,
            "--message", message,
            "--session-id", session_id,
            "--timeout", "300"
        ]
        print(f"🤖 调用 Agent: {agent}, session: {session_id}")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode == 0:
            response = result.stdout.strip()
            print(f"✅ Agent 响应: {len(response)} 字符")
            return response
        else:
            error = result.stderr or result.stdout
            print(f"❌ Agent 错误: {error[:200]}")
            return "❌ AI 暂时无法响应，请稍后重试"
    except subprocess.TimeoutExpired:
        return "⏰ 响应超时，请简化问题后重试"
    except Exception as e:
        return f"❌ 调用异常: {str(e)}"


# 初始化命令处理模块（需要 call_openclaw_agent 已定义）
from command_handler import init_command_handler
init_command_handler(
    reply_message, reply_card, call_openclaw_agent, query_knowledge_base,
    get_memory, add_to_history, get_history,
    is_article_url, is_video_url, summarize_url, handle_video_async,
    extract_video_url, TaskLogger, detect_chain_intent, execute_chain,
    IntentRouter, save_active_user, load_active_user,
    chat_history, pending_voice, pending_image, pending_review,
    MAX_HISTORY, RATE_LIMIT_SECONDS, user_last_request, memory_cache
)

# ========== 消息分发 ==========

def do_p2_im_message_receive_v1(data) -> None:
    global processed_messages, connection_status
    # 更新最后事件时间以监控连接状态
    connection_status["last_event"] = time.time()
    connection_status["connected"] = True  # 确保在收到消息时设置为连接状态

    try:
        event = data.event
        message = event.message
        message_id = message.message_id

        cleanup_pending_images()

        # 去重
        if message_id in processed_messages:
            return
        processed_messages.add(message_id)

        # 限流
        sender = event.sender
        temp_user_id = "unknown"
        if sender and sender.sender_id:
            temp_user_id = sender.sender_id.user_id or sender.sender_id.open_id or "unknown"
        if not check_rate_limit(temp_user_id):
            print(f"⚠️ 限流: {temp_user_id}")
            return

        if len(processed_messages) > 1000:
            keep = list(processed_messages)[-500:]; processed_messages.clear(); processed_messages.update(keep)

        msg_type = message.message_type
        content_str = message.content

        user_id = "unknown"
        if sender and sender.sender_id:
            user_id = sender.sender_id.user_id or sender.sender_id.open_id or sender.sender_id.union_id or "unknown"

        print(f"\n{'=' * 50}")
        print(f"📨 [{msg_type}] 用户: {str(user_id)[:10]}...")

        # T-056: 持久化最近活跃用户
        save_active_user(user_id)

        content_dict = json.loads(content_str)

        if msg_type == "text":
            text = content_dict.get("text", "")
            text = re.sub(r'@_user_\d+\s*', '', text).strip()
            print(f"   文本: {text[:50]}...")
            if text:
                thread = threading.Thread(
                    target=handle_text_async,
                    args=(text, user_id, message_id)
                )
                thread.start()

        elif msg_type == "audio":
            reply_message(message_id,
                "🎤 语音识别功能暂时关闭\n\n请直接发送文字消息")

        elif msg_type == "image":
            image_key = content_dict.get("image_key", "")
            print(f"   图片: {image_key[:30]}...")
            reply_message(message_id, "🖼️ 正在分析图片，请稍候...")
            thread = threading.Thread(
                target=handle_image_async,
                args=(message_id, image_key, user_id)
            )
            thread.start()

        elif msg_type in ["media", "file"]:
            reply_message(message_id,
                "📁 暂不支持该文件类型\n\n支持：文字 | 图片 | 网页链接 | 视频链接")

        else:
            print(f"   ⏭️ 不支持: {msg_type}")

        print(f"{'=' * 50}")

    except Exception as e:
        print(f"❌ 处理错误: {e}")
        import traceback
        traceback.print_exc()


def do_p2_card_action_trigger_v1(data) -> None:
    """处理卡片交互事件 (审批按钮)"""
    try:
        action = data.action
        value = action.value  # 这是一个 dict，包含在卡片定义中
        user_id = data.operator.user_id
        
        action_type = value.get("action")
        task_id = value.get("task_id")
        plan_name = value.get("plan_name")
        
        print(f"🔘 卡片交互: {action_type} for {plan_name or task_id} by {user_id}")
        
        if action_type in ["approve", "reject"]:
            # 发送响应消息到 MessageBus
            mb = MessageBus()
            mb.publish(
                sender=f"bot_{user_id}",
                topic="plan_approval",
                content=action_type,
                metadata={
                    "task_id": task_id,
                    "plan_name": plan_name,
                    "user_id": user_id,
                    "timestamp": time.time()
                }
            )
            
            # 更新卡片状态（可选，这里先简单回复一条消息）
            reply_message(data.context.open_message_id, f"✅ 已收到您的「{ '批准' if action_type == 'approve' else '拒绝' }」操作。正在处理中...")
            
    except Exception as e:
        print(f"❌ 处理卡片回调失败: {e}")

def main():
    event_handler = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(do_p2_im_message_receive_v1) \
        .register_p2_card_action_trigger(do_p2_card_action_trigger_v1) \
        .build()

    cli = lark.ws.Client(
        APP_ID,
        APP_SECRET,
        event_handler=event_handler,
        log_level=lark.LogLevel.INFO
    )

    # P5 优化：monkey-patch _configure，防止服务端覆盖 ping 间隔
    _original_configure = cli._configure
    def _patched_configure(conf):
        _original_configure(conf)
        # ISSUE-003 优化：缩短 ping 间隔以避免超时断连
        # 根据最近的连接日志分析，将 ping 间隔进一步调整为 10 秒以应对高延迟网络
        cli._ping_interval = 10  # 从 15 秒缩短到 10 秒，提供更频繁的心跳
        cli._reconnect_interval = 8  # 调整重连间隔以平衡重连速度和服务器压力
        cli._reconnect_nonce = 10  # 增加重试次数，允许更多重连尝试
        print(f"🔄 WebSocket 配置更新: ping间隔={cli._ping_interval}s, 重连间隔={cli._reconnect_interval}s, 重连次数={cli._reconnect_nonce}")
    cli._configure = _patched_configure

    print("🤖 知微 v2.1 启动 (RAG增强版)")
    print("   新增: 知识库检索 (/ask 或 '查一下')")
    print("   特性: 三层记忆 | 意图路由 | 任务日志")
    print("   支持: 文字 | 图片 | 网页链接 | 视频链接")
    print("-" * 50)

    # ISSUE-003: 断连监控和告警线程
    import threading
    import time
    from datetime import datetime

    # 全局变量用于监控连接状态
    connection_status = {"connected": True, "last_event": time.time()}

    def connection_monitor():
        """监控连接状态，检测异常断连"""
        last_status = True
        disconnect_count = 0

        while True:
            current_time = time.time()
            # 如果超过60秒没有收到任何事件，则认为可能有问题
            if current_time - connection_status["last_event"] > 60:
                if connection_status["connected"]:
                    print(f"⚠️ 检测到可能的连接问题 - 超过60秒无事件")
                    # 这里可以增加更多诊断逻辑
                    connection_status["connected"] = False
                    disconnect_count += 1
                    # 记录到日志用于分析
                    with open(os.path.expanduser("~/logs/connection_monitor.log"), "a") as f:
                        f.write(f"{datetime.now().isoformat()}: Possible disconnection detected. Disconnect count: {disconnect_count}\n")

            time.sleep(30)  # 每30秒检查一次

    # 启动监控线程
    monitor_thread = threading.Thread(target=connection_monitor, daemon=True)
    monitor_thread.start()

    # 原 poll_review_notifications 和 poll_task_notifications 已废弃
    # 逻辑整合入 poll_message_bus

    # T-056: 审批通知轮询线程
    # poll_thread = threading.Thread(target=poll_review_notifications, daemon=True)
    # poll_thread.start()

    # T-055: 启动任务通知轮询线程
    # task_notify_thread = threading.Thread(target=poll_task_notifications, daemon=True)
    # task_notify_thread.start()

    # 启动统一的 MessageBus 消费线程 (取代原有的文件轮询)
    def poll_message_bus():
        import feishu_api as _feishu_api_mod
        mb = MessageBus()
        print("💡 MessageBus 消费线程已启动")
        while True:
            try:
                # 消费飞书通知和审批主题
                topics = ["feishu_notification", "feishu_card_notification"]
                for topic in topics:
                    messages = mb.consume_pending(topic=topic, limit=5)
                    if messages:
                        print(f"📥 MessageBus: 发现 {len(messages)} 条新消息 ({topic})")
                    for msg in messages:
                        print(f"🔄 MessageBus: 正在处理消息 {msg['id']}...")
                        try:
                            meta = json.loads(msg["metadata"] or "{}")
                            target_user = meta.get("user_id") or last_active_user.get("user_id") or load_active_user()
                            
                            if not target_user:
                                print(f"⚠️ MessageBus: 消息 {msg['id']} 找不到目标飞书用户")
                                continue

                            if topic == "feishu_notification":
                                # 普通文本消息
                                success = _feishu_api_mod.send_direct_message(target_user, msg["content"])
                            else:
                                # 卡片消息 (metadata 中包含卡片 JSON)
                                card_content = msg["content"]
                                try:
                                    from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
                                    request = CreateMessageRequest.builder() \
                                        .receive_id_type("open_id") \
                                        .request_body(CreateMessageRequestBody.builder()
                                            .receive_id(target_user)
                                            .content(card_content)
                                            .msg_type("interactive")
                                            .build()) \
                                        .build()
                                    response = client.im.v1.message.create(request)
                                    success = response.success()
                                except: success = False

                            if success:
                                mb.mark_sent(msg["id"])
                                print(f"✅ MessageBus: [{topic}] 消息 {msg['id']} 已推送到飞书")
                            else:
                                mb.mark_failed(msg["id"], "Feishu API delivery failed")
                        except Exception as e:
                            print(f"❌ 处理 MessageBus 消息异常: {e}")
                time.sleep(2)
            except Exception as e:
                print(f"❌ MessageBus 消费异常: {e}")
                time.sleep(5)

    msg_bus_thread = threading.Thread(target=poll_message_bus, daemon=True)
    msg_bus_thread.start()

    cli.start()


if __name__ == "__main__":
    main()
