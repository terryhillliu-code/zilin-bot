"""
知微 v2.2 - 飞书机器人 (RAG 增强版)
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
from concurrent.futures import ThreadPoolExecutor
import signal

# 导入新模块
from memory_manager import MemoryManager
from task_logger import TaskLogger
from intent_router import IntentRouter
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

# 消息去重 (deque 自动淘汰旧消息)
processed_messages = deque(maxlen=500)

# 线程池（最大 10 个并发任务）
executor = ThreadPoolExecutor(max_workers=10, thread_name_prefix="msg_handler")

# 连接状态监控 (ISSUE-003 / ISSUE-027 修复)
# 简化版：仅监控业务事件，避免误判
connection_status = {
    "connected": True,
    "last_event": time.time(),       # 业务事件（收到消息）
}

# ========== WebSocket 心跳监控 (v44.4) ==========
HEARTBEAT_FILE = os.path.expanduser("~/logs/ws_heartbeat.json")

def write_heartbeat(conn_id: str = "", status: str = "connected"):
    """写入心跳状态文件，供 watchdog 检测"""
    try:
        with open(HEARTBEAT_FILE, "w") as f:
            json.dump({
                "timestamp": time.time(),
                "conn_id": conn_id,
                "status": status
            }, f)
    except Exception:
        pass

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

def query_knowledge_base(query: str, top_k: int = 3) -> str:
    """通过子进程调用 bridge.py 检索知识库 (V2-204-Fix2: 隔离依赖环境)"""
    import subprocess
    try:
        print(f"📚 RAG 检索 (子进程)：{query}")
        rag_venv = "/Users/liufang/zhiwei-rag/venv/bin/python3"
        bridge_script = "/Users/liufang/zhiwei-rag/bridge.py"
        
        result = subprocess.run(
            [rag_venv, bridge_script, "context", query, "--top-k", str(top_k)],
            capture_output=True, text=True, timeout=40
        )
        if result.returncode == 0:
            return result.stdout.strip()
        else:
            print(f"❌ RAG 子进程失败：{result.stderr}")
            return None
    except Exception as e:
        print(f"❌ RAG 调用异常：{e}")
        return None




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
init_media_handler(client, reply_message, TaskLogger, pending_image, pending_voice, time)


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


# V2-203: 导入 ChatHandler 替代 OpenClaw
from chat_handler import ChatHandler, chat_handler as _chat_handler_instance

def get_chat_handler():
    """获取 ChatHandler 实例"""
    return _chat_handler_instance


def call_openclaw_agent(message: str, session_id: str, agent: str = "main") -> str:
    """
    调用 OpenClaw Agent

    @deprecated V2-203: 已废弃，请使用 get_chat_handler().handle_sync()
    保留此函数是为了向后兼容，实际调用已切换到 chat_handler
    """
    # 降级：使用 chat_handler
    handler = get_chat_handler()
    return handler.handle_sync(message, session_id, role=agent)

# 初始化命令处理模块（需要 call_openclaw_agent 已定义）
from command_handler import init_command_handler
init_command_handler(
    reply_message, reply_card, call_openclaw_agent, query_knowledge_base,
    get_memory, add_to_history, get_history,
    is_article_url, is_video_url, summarize_url, handle_video_async,
    extract_video_url, extract_article_url, TaskLogger,
    IntentRouter, save_active_user, load_active_user,
    chat_history, pending_voice, pending_image, pending_review,
    MAX_HISTORY, RATE_LIMIT_SECONDS, user_last_request, memory_cache,
    get_chat_handler,  # V2-203: 新增 chat_handler
    global_pending_video_confirm=pending_video_confirm  # 视频重复确认
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
        except:
            pass

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
                    offline_recovery.cache_chat_id(temp_user_id, message.chat_id)

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

        # T-056: 持久化最近活跃用户
        save_active_user(user_id)

        content_dict = json.loads(content_str)

        if msg_type == "text":
            text = content_dict.get("text", "")
            text = re.sub(r'@_user_\d+\s*', '', text).strip()
            print(f"   文本：{text[:50]}...")
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


def do_p2_card_action_trigger_v1(data) -> None:
    """处理卡片交互事件 (审批按钮 + v47.0 研究卡片)"""
    try:
        action = data.action
        value = action.value  # 这是一个 dict，包含在卡片定义中
        user_id = data.operator.user_id
        message_id = data.context.open_message_id

        action_type = value.get("action")
        task_id = value.get("task_id")
        plan_name = value.get("plan_name")

        print(f"🔘 卡片交互：{action_type} for {plan_name or task_id} by {user_id}")

        # ⭐ v47.0: 研究卡片回调处理
        if action_type == "start_research":
            topic = value.get("topic", "")
            include_videos = value.get("include_videos", "true").lower() == "true"

            print(f"[WSClient] 研究卡片确认: topic={topic}, videos={include_videos}")

            reply_message(message_id, f"🚀 正在为您准备「{topic}」的研究素材...")

            # 触发研究执行器
            from core.research_report_executor import research_executor
            research_topic = topic
            if include_videos:
                research_topic += " --include-videos"

            threading.Thread(
                target=research_executor.execute,
                args=(research_topic, user_id, message_id, reply_message, reply_card),
                daemon=True
            ).start()
            return

        elif action_type == "cancel_research":
            reply_message(message_id, "✅ 已取消研究")
            return

        elif action_type == "show_config_form":
            # TODO: 显示详细配置表单
            reply_message(message_id, "⚙️ 详细配置功能开发中...")
            return

        # 原有审批逻辑
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
            reply_message(message_id, f"✅ 已收到您的「{ '批准' if action_type == 'approve' else '拒绝' }」操作。正在处理中...")

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

    # P5 优化：monkey-patch _configure，防止服务端覆盖 ping 间隔
    _original_configure = cli._configure
    
    def _patched_configure(conf):
        _original_configure(conf)
        # ISSUE-003 优化：缩短 ping 间隔以避免超时断连
        cli._ping_interval = 10 
        cli._reconnect_interval = 8
        cli._reconnect_nonce = 10
        print(f"🔄 WebSocket 配置更新：ping 间隔={cli._ping_interval}s, 重连间隔={cli._reconnect_interval}s")
    
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

    # ISSUE-003: 断连监控和告警线程
    from datetime import datetime

    # 全局变量用于监控连接状态
    # 告警状态文件路径
    ALERT_STATE_FILE = os.path.expanduser("~/logs/ws_alert_state.json")

    def load_alert_state() -> dict:
        """加载告警状态"""
        try:
            if os.path.exists(ALERT_STATE_FILE):
                with open(ALERT_STATE_FILE) as f:
                    return json.load(f)
        except:
            pass
        return {"last_alert_time": 0, "alert_type": None}

    def save_alert_state(state: dict):
        """保存告警状态"""
        try:
            with open(ALERT_STATE_FILE, "w") as f:
                json.dump(state, f)
        except:
            pass

    def send_ws_alert(msg: str, alert_type: str = "disconnect") -> bool:
        """发送 WebSocket 告警（通过钉钉，避免消耗飞书额度）"""
        state = load_alert_state()
        now = time.time()

        # 告警频率控制：同一类型告警每小时最多发一次
        if state.get("alert_type") == alert_type:
            if now - state.get("last_alert_time", 0) < 3600:
                print(f"⏭️ 告警频率限制，跳过推送：{alert_type}")
                return False

        # 尝试通过钉钉发送
        try:
            sys.path.insert(0, os.path.expanduser("~/zhiwei-scheduler"))
            from pusher import DingTalkPusher
            import yaml

            config_path = os.path.expanduser("~/zhiwei-scheduler/config/settings.yaml")
            with open(config_path) as f:
                dt_conf = yaml.safe_load(f).get("push", {}).get("dingtalk", {})

            if dt_conf.get("enabled"):
                pusher = DingTalkPusher(dt_conf["webhook"], dt_conf["secret"])
                pusher.send_text(msg)
                print(f"📱 WebSocket 告警已发送: {msg[:50]}...")

                # 更新告警状态
                save_alert_state({
                    "last_alert_time": now,
                    "alert_type": alert_type
                })
                return True
            else:
                print("⚠️ 钉钉未启用，无法发送告警")
        except Exception as e:
            print(f"❌ 发送 WebSocket 告警失败: {e}")

        return False

    def connection_monitor():
        """连接监控线程 - 优化版 (v44.5)

        功能：
        1. 每分钟写入心跳文件（供 watchdog 检测）
        2. 业务消息空闲时记录日志（不发送钉钉告警，避免误报）
        3. ⭐ 离线恢复检测：长时间空闲后恢复时尝试恢复离线消息
        """
        # 启动时立即写入心跳
        write_heartbeat(status="starting")

        # 离线检测状态
        was_idle_long = False  # 上一次检查时是否长时间空闲

        while True:
            time.sleep(60)  # 每分钟检查一次
            now = time.time()
            event_idle = now - connection_status.get("last_event", now)

            # 写入心跳（即使空闲也写入，表示服务存活）
            write_heartbeat(status="connected")

            # 检测长时间空闲（超过 5 分钟）
            is_idle_long = event_idle > 300  # 5 分钟

            # ⭐ 离线恢复检测：从长时间空闲恢复到活跃
            if was_idle_long and not is_idle_long:
                # 刚从长时间空闲恢复，尝试离线恢复
                offline_recovery = get_offline_recovery()
                if offline_recovery and offline_recovery.should_recover(threshold_seconds=300):
                    idle_minutes = int(event_idle / 60)
                    print(f"🔄 检测到离线恢复（空闲 {idle_minutes} 分钟），尝试恢复离线消息...")

                    # 获取最近活跃用户
                    active_user = load_active_user()
                    if active_user:
                        try:
                            # 获取私聊会话 ID
                            chat_id = offline_recovery.get_p2p_chat_id(active_user)
                            if chat_id:
                                # 恢复离线消息
                                since_time = offline_recovery.state.get("last_disconnect_time", time.time() - 3600)
                                messages = offline_recovery.recover_messages(chat_id, since_time)
                                if messages:
                                    print(f"📬 恢复了 {len(messages)} 条离线消息")
                                    # 处理恢复的消息（模拟消息事件）
                                    for msg in messages[-5:]:  # 最多处理最近 5 条
                                        print(f"   📨 离线消息: {msg.content[:50] if msg.content else 'N/A'}...")
                        except Exception as e:
                            print(f"⚠️ 离线恢复失败: {e}")

                    # 记录重连时间
                    offline_recovery.record_reconnect()

            # 更新空闲状态
            was_idle_long = is_idle_long

            # 长时间空闲时记录断连时间
            if is_idle_long and not was_idle_long:
                offline_recovery = get_offline_recovery()
                if offline_recovery:
                    offline_recovery.record_disconnect()

            # 业务消息空闲超过 30 分钟才记录日志（不再发送钉钉告警）
            if event_idle > 1800:  # 30 分钟
                idle_minutes = int(event_idle / 60)

                # 仅记录到日志，不发送钉钉告警
                with open(os.path.expanduser("~/logs/connection_monitor.log"), "a") as f:
                    f.write(f"{datetime.now().isoformat()}: Business idle {idle_minutes}min (normal)\n")

                print(f"💡 业务消息空闲 {idle_minutes} 分钟（正常现象，连接通过 ping 保持）")

                # 重置时间戳，避免频繁记录日志
                connection_status["last_event"] = now

    # 启动监控线程
    monitor_thread = threading.Thread(target=connection_monitor, daemon=True)
    monitor_thread.start()

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