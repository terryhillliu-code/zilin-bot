from openclaw_api import OpenClawClient
"""
知微 v2.1 - OpenClaw Agent 飞书机器人 (RAG 增强版)
新增特性：
- 📚 知识库检索 (RAG)：支持 /ask 命令和智能触发
- 🧠 集成 klib 向量数据库
"""

# 加载环境变量文件 (必须在最前面，在导入其他模块之前)
from dotenv import load_dotenv
from pathlib import Path
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    load_dotenv(env_path)
    print(f"✅ 已加载环境变量：{env_path}")
else:
    print(f"⚠️ 未找到 .env 文件，使用默认配置：{env_path}")

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

# 连接状态监控 (ISSUE-003 / ISSUE-027 修复)
# 区分心跳事件和业务事件，避免把"没有用户消息"误判为连接断开
connection_status = {
    "connected": True,
    "last_event": time.time(),       # 业务事件（收到消息）
    "last_heartbeat": time.time(),   # 心跳事件（ping/pong）
}

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
        print(f"📚 RAG 检索：{query}")
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
            print(f"❌ RAG 检索失败：{result.stderr[:200]}")
            return None

    except subprocess.TimeoutExpired:
        print("❌ RAG 检索超时")
        return None
    except Exception as e:
        print(f"❌ RAG 调用异常：{e}")
        return None

# ========== 应用配置 ==========

# ========== 应用配置 ==========

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
from media_handler import init_media_handler
init_media_handler(client, reply_message, TaskLogger, pending_image, time)


def get_memory(user_id: str) -> MemoryManager:
    """获取或创建用户的记忆管理器"""
    if user_id not in memory_cache:
        memory_cache[user_id] = MemoryManager(user_id)
    return memory_cache[user_id]


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
            print(f"✅ Agent 响应：{len(response)} 字符")
            return response
        else:
            error = result.stderr or result.stdout
            print(f"❌ Agent 错误：{error[:200]}")
            return "❌ AI 暂时无法响应，请稍后重试"
    except subprocess.TimeoutExpired:
        return "⏰ 响应超时，请简化问题后重试"
    except Exception as e:
        return f"❌ 调用异常：{str(e)}"


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

    try:
        event = data.event
        message = event.message

        cleanup_pending_images()

        # 去重
        if message_id in processed_messages:
            print(f"⏭️ 消息 {message_id} 已处理过，跳过")
            return
        processed_messages.add(message_id)

        # 限流 & 识别用户
        sender = event.sender
        temp_user_id = "unknown"
        if sender and sender.sender_id:
            # 优先使用 open_id，因为它在 API 调用中更通用且需要权限较少
            temp_user_id = sender.sender_id.open_id or sender.sender_id.user_id or "unknown"
        
        # 保存最近活跃用户
        if temp_user_id != "unknown":
            save_active_user(temp_user_id)

        if not check_rate_limit(temp_user_id):
            print(f"⚠️ 限流：{temp_user_id}")
            return

        if len(processed_messages) > 1000:
            keep = list(processed_messages)[-500:]; processed_messages.clear(); processed_messages.update(keep)

        msg_type = message.message_type
        content_str = message.content

        user_id = "unknown"
        if sender and sender.sender_id:
            user_id = sender.sender_id.user_id or sender.sender_id.open_id or sender.sender_id.union_id or "unknown"

        print(f"\n{'=' * 50}")
        print(f"📨 [{msg_type}] 用户：{str(user_id)[:10]}...")

        # T-056: 持久化最近活跃用户
        save_active_user(user_id)

        content_dict = json.loads(content_str)

        if msg_type == "text":
            text = content_dict.get("text", "")
            text = re.sub(r'@_user_\d+\s*', '', text).strip()
            print(f"   文本：{text[:50]}...")
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
            print(f"   图片：{image_key[:30]}...")
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
            print(f"   ⏭️ 不支持：{msg_type}")

        print(f"{'=' * 50}")

    except Exception as e:
        print(f"❌ 处理错误：{e}")
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
        
        print(f"🔘 卡片交互：{action_type} for {plan_name or task_id} by {user_id}")
        
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
        print(f"❌ 处理卡片回调失败：{e}")

def main():
    event_handler = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(do_p2_im_message_receive_v1) \
        .register_p2_card_action_trigger(do_p2_card_action_trigger_v1) \
        .build()

    print(f"🔧 启动 WebSocket 客户端 (AppID: {APP_ID})")
    cli = lark.ws.Client(
        APP_ID,
        APP_SECRET,
        event_handler=event_handler,
        log_level=lark.LogLevel.DEBUG
    )

    # P5 优化：monkey-patch _configure，防止服务端覆盖 ping 间隔
    _original_configure = cli._configure
    
    # 保存原始的 _send_ping 方法
    _original_send_ping = None
    
    def _patched_configure(conf):
        global _original_send_ping
        _original_configure(conf)
        # ISSUE-003 优化：缩短 ping 间隔以避免超时断连
        # 根据最近的连接日志分析，将 ping 间隔进一步调整为 10 秒以应对高延迟网络
        cli._ping_interval = 10  # 从 15 秒缩短到 10 秒，提供更频繁的心跳
        cli._reconnect_interval = 8  # 调整重连间隔以平衡重连速度和服务器压力
        cli._reconnect_nonce = 10  # 增加重试次数，允许更多重连尝试
        print(f"🔄 WebSocket 配置更新：ping 间隔={cli._ping_interval}s, 重连间隔={cli._reconnect_interval}s, 重连次数={cli._reconnect_nonce}")
        
        # ISSUE-027 修复：monkey-patch _send_ping 和 _on_pong，更新心跳时间
        if hasattr(cli, '_send_ping') and _original_send_ping is None:
            _original_send_ping = cli._send_ping
            def _patched_send_ping():
                _original_send_ping()
                # 发送 ping 后更新心跳时间
                connection_status["last_heartbeat"] = time.time()
            cli._send_ping = _patched_send_ping
    
    cli._configure = _patched_configure

    print("🤖 知微 v2.1 启动 (RAG 增强版)")
    print("   新增：知识库检索 (/ask 或 '查一下')")
    print("   特性：三层记忆 | 意图路由 | 任务日志")
    print("   支持：文字 | 图片 | 网页链接 | 视频链接")
    print("-" * 50)

    # ISSUE-003: 断连监控和告警线程
    import threading
    import time
    from datetime import datetime

    # 全局变量用于监控连接状态

    def connection_monitor():
        """监控连接状态，检测异常断连 (ISSUE-027 修复版)
        
        修复：区分心跳事件和业务事件
        - 心跳超时 = 真的断了 → 触发重启
        - 业务消息空闲 = 正常 → 只告警不重启
        """
        last_status = True
        disconnect_count = 0
        last_reconnect_time = 0
        RECONNECT_COOLDOWN = 1800  # 重连冷却时间 30 分钟（防止频繁重启）

        while True:
            time.sleep(30)
            now = time.time()
            
            # 分别检查心跳和业务事件
            heartbeat_idle = now - connection_status.get("last_heartbeat", now)
            event_idle = now - connection_status.get("last_event", now)
            
            # 心跳超时 120 秒 → 真的断了，触发重启
            if heartbeat_idle > 120:
                # 检查冷却时间
                if now - last_reconnect_time < RECONNECT_COOLDOWN:
                    print(f"🟠 连接监控：处于重连冷却期，跳过强制重连（心跳超时 {heartbeat_idle:.0f}s）")
                    continue
                    
                print(f"🔴 连接监控：心跳超时 {heartbeat_idle:.0f}秒，触发强制重启")
                last_reconnect_time = now
                disconnect_count += 1
                
                # 记录到日志
                with open(os.path.expanduser("~/logs/connection_monitor.log"), "a") as f:
                    f.write(f"{datetime.now().isoformat()}: Force reconnect - heartbeat timeout {heartbeat_idle:.0f}s. Disconnect count: {disconnect_count}\n")
                
                # 触发进程重启
                try:
                    print("🔄 执行 launchctl kickstart 重启服务...")
                    os.system("launchctl kickstart -k gui/$(id -u)/com.zhiwei.bot")
                    # 重启命令发出后退出当前监控线程
                    break
                except Exception as e:
                    print(f"❌ 重启失败：{e}")
            
            # 业务消息空闲 5 分钟 → 只告警，不重启
            elif event_idle > 300:
                if connection_status["connected"]:
                    print(f"🟡 连接监控：业务消息空闲 5 分钟，连接正常 (心跳={heartbeat_idle:.0f}s)")
                    connection_status["connected"] = False  # 标记为空闲状态
                    
                    with open(os.path.expanduser("~/logs/connection_monitor.log"), "a") as f:
                        f.write(f"{datetime.now().isoformat()}: Business idle 5min - connection OK (heartbeat={heartbeat_idle:.0f}s)\n")
            
            # 连接恢复
            elif event_idle <= 60 or heartbeat_idle <= 60:
                if not connection_status["connected"]:
                    connection_status["connected"] = True
                    print(f"✅ 连接监控：连接已恢复 (业务 idle={event_idle:.0f}s, 心跳 idle={heartbeat_idle:.0f}s)")

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

    # 启动统一的 MessageBus 消费线程
    def poll_message_bus():
        import feishu_api as _feishu_api_mod
        mb = MessageBus()
        print("💡 MessageBus 消费线程已启动")
        
        # 启动时发布一条自检消息
        try:
            mb.publish(
                sender="bot/startup",
                topic="feishu_notification",
                content=f"🤖知微机器人已重启\n时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\nAppID: {APP_ID[:8]}...",
                metadata={"user_id": load_active_user()}
            )
        except: pass

        while True:
            try:
                # 消费飞书通知和审批主题
                topics = ["feishu_notification", "feishu_card_notification"]
                for topic in topics:
                    try:
                        messages = mb.consume_pending(topic=topic, limit=5)
                        if messages:
                            print(f"📥 MessageBus: 发现 {len(messages)} 条新消息 ({topic})")
                        for msg in messages:
                            print(f"🔄 MessageBus: 正在处理消息 {msg['id']}...")
                            try:
                                meta = json.loads(msg["metadata"] or "{}")
                                target_user = meta.get("user_id") or last_active_user.get("user_id") or load_active_user()
                                
                                if not target_user:
                                    print(f"⚠️ MessageBus: 消息 {msg['id']} 找不到目标用户，标记为失败")
                                    mb.mark_failed(msg["id"], "No target user found")
                                    continue

                                if topic == "feishu_notification":
                                    success = _feishu_api_mod.send_direct_message(target_user, msg["content"])
                                else:
                                    # 卡片消息 - 自动识别 ID 类型
                                    id_type = "user_id"
                                    if target_user.startswith("ou_"):
                                        id_type = "open_id"
                                    elif target_user.startswith("on_"):
                                        id_type = "union_id"
                                    elif target_user.startswith("oc_"):
                                        id_type = "chat_id"

                                    request = CreateMessageRequest.builder() \
                                        .receive_id_type(id_type) \
                                        .request_body(CreateMessageRequestBody.builder()
                                            .receive_id(target_user)
                                            .content(msg["content"])
                                            .msg_type("interactive")
                                            .build()) \
                                        .build()
                                    response = client.im.v1.message.create(request)
                                    success = response.success()

                                if success:
                                    mb.mark_sent(msg["id"])
                                    print(f"✅ MessageBus: 消息 {msg['id']} 已成功推送到飞书")
                                else:
                                    mb.mark_failed(msg["id"], "Feishu API delivery failed")
                                    print(f"❌ MessageBus: 消息 {msg['id']} 推送失败")
                            except Exception as em:
                                print(f"❌ MessageBus: 处理单条消息 {msg['id']} 异常：{em}")
                                mb.mark_failed(msg["id"], str(em))
                    except Exception as et:
                        print(f"❌ MessageBus: [Topic: {topic}] 轮询异常：{et}")
                time.sleep(2)
            except Exception as e:
                print(f"❌ MessageBus: 主循环异常：{e}")
                time.sleep(5)

    msg_bus_thread = threading.Thread(target=poll_message_bus, daemon=True)
    msg_bus_thread.start()

    cli.start()


if __name__ == "__main__":
    main()