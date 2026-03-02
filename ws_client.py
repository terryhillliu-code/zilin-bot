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

# 导入命令处理模块
from command_handler import handle_text_async, show_help, get_session_id, get_quick_status, check_rate_limit

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

APP_ID = "cli_a9142bd071bd1bd9"
APP_SECRET = "mlIZdNRvxpaVQIB6VQxHIee6WgW4UcPf"

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
    global processed_messages
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


def main():
    event_handler = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(do_p2_im_message_receive_v1) \
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
        cli._ping_interval = 30
        cli._reconnect_interval = 10
        cli._reconnect_nonce = 5
    cli._configure = _patched_configure

    print("🤖 知微 v2.1 启动 (RAG增强版)")
    print("   新增: 知识库检索 (/ask 或 '查一下')")
    print("   特性: 三层记忆 | 意图路由 | 任务日志")
    print("   支持: 文字 | 图片 | 网页链接 | 视频链接")
    print("-" * 50)

    # T-056: 审批通知轮询线程
    def poll_review_notifications():
        """轮询 ~/tasks/review/*.notify 文件，推送交互式卡片到飞书"""
        review_dir = os.path.expanduser("~/tasks/review")
        os.makedirs(review_dir, exist_ok=True)
        load_active_user()

        while True:
            try:
                for fname in os.listdir(review_dir):
                    if not fname.endswith(".notify"):
                        continue

                    notify_path = os.path.join(review_dir, fname)
                    try:
                        with open(notify_path) as f:
                            notify_data = json.load(f)

                        task_id = notify_data.get("task_id", "")
                        message = notify_data.get("message", "")
                        risk_level = notify_data.get("risk_level", "medium")

                        if not message:
                            continue

                        target_user = last_active_user.get("user_id") or load_active_user()
                        if not target_user:
                            print(f"⏳ 审批通知 {task_id} 等待活跃用户...")
                            continue

                        # 构建交互式卡片
                        risk_color = "red" if risk_level == "high" else "orange"
                        card = {
                            "config": {"wide_screen_mode": True},
                            "header": {
                                "title": {"tag": "plain_text", "content": "🔔 开发任务待审批"},
                                "template": risk_color
                            },
                            "elements": [
                                {
                                    "tag": "markdown",
                                    "content": message.replace("回复「批准", "点击按钮").replace("」执行 | 回复「取消", "").replace("」放弃", "")
                                },
                                {
                                    "tag": "action",
                                    "actions": [
                                        {
                                            "tag": "button",
                                            "text": {"tag": "plain_text", "content": "✅ 批准执行"},
                                            "type": "primary",
                                            "value": {"action": "approve", "task_id": task_id}
                                        },
                                        {
                                            "tag": "button",
                                            "text": {"tag": "plain_text", "content": "❌ 取消任务"},
                                            "type": "danger",
                                            "value": {"action": "reject", "task_id": task_id}
                                        }
                                    ]
                                }
                            ]
                        }

                        # 发送卡片消息
                        try:
                            from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
                            content = json.dumps(card)
                            request = CreateMessageRequest.builder() \
                                .receive_id_type("user_id") \
                                .request_body(CreateMessageRequestBody.builder()
                                    .receive_id(target_user)
                                    .content(content)
                                    .msg_type("interactive")
                                    .build()) \
                                .build()
                            response = client.im.v1.message.create(request)
                            if response.success():
                                print(f"✅ 审批卡片已推送: {task_id}")
                                pending_review[target_user] = task_id
                                os.remove(notify_path)
                            else:
                                print(f"❌ 卡片推送失败: {response.code} - {response.msg}")
                        except Exception as e:
                            print(f"❌ 卡片推送异常: {e}")

                    except Exception as e:
                        print(f"❌ 处理通知文件异常: {e}")
            except Exception as e:
                pass

            time.sleep(5)

    # T-055: 任务完成通知轮询线程
    def poll_task_notifications():
        """轮询 ~/tasks/notify/*.json 文件，推送任务结果到飞书"""
        notify_dir = os.path.expanduser("~/tasks/notify")
        os.makedirs(notify_dir, exist_ok=True)
        load_active_user()

        while True:
            try:
                for fname in os.listdir(notify_dir):
                    if not fname.endswith(".json"):
                        continue

                    notify_path = os.path.join(notify_dir, fname)
                    try:
                        with open(notify_path) as f:
                            notify_data = json.load(f)

                        task_id = notify_data.get("task_id", "")
                        feishu_user_id = notify_data.get("feishu_user_id", "")
                        status = notify_data.get("status", "unknown")
                        title = notify_data.get("title", "开发任务")
                        summary = notify_data.get("summary", "任务已完成")

                        if not task_id:
                            continue

                        # 使用任务中指定的飞书用户ID，如果没有则使用最近活跃用户
                        target_user = feishu_user_id or last_active_user.get("user_id") or load_active_user()
                        if not target_user:
                            print(f"⏳ 任务通知 {task_id} 等待飞书用户...")
                            continue

                        # 构建结果卡片
                        status_emoji = "✅" if status == "success" else "❌"
                        status_color = "green" if status == "success" else "red"
                        status_text = "已完成" if status == "success" else "失败"

                        card = {
                            "config": {"wide_screen_mode": True},
                            "header": {
                                "title": {"tag": "plain_text", "content": f"{status_emoji} 任务 {status_text}"},
                                "template": status_color
                            },
                            "elements": [
                                {
                                    "tag": "div",
                                    "text": {"tag": "plain_text", "content": f"📋 任务ID: {task_id}"}
                                },
                                {
                                    "tag": "div",
                                    "text": {"tag": "plain_text", "content": f"📌 标题: {title}"}
                                },
                                {
                                    "tag": "div",
                                    "text": {"tag": "plain_text", "content": f"📝 结果: {summary[:200]}"}
                                }
                            ]
                        }

                        # 发送卡片消息
                        try:
                            from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
                            content = json.dumps(card)
                            request = CreateMessageRequest.builder() \
                                .receive_id_type("user_id") \
                                .request_body(CreateMessageRequestBody.builder()
                                    .receive_id(target_user)
                                    .content(content)
                                    .msg_type("interactive")
                                    .build()) \
                                .build()
                            response = client.im.v1.message.create(request)
                            if response.success():
                                print(f"✅ 任务通知已推送: {task_id}")
                                os.remove(notify_path)
                            else:
                                print(f"❌ 任务通知推送失败: {response.code} - {response.msg}")
                        except Exception as e:
                            print(f"❌ 任务通知推送异常: {e}")

                    except Exception as e:
                        print(f"❌ 处理任务通知文件异常: {e}")
            except Exception as e:
                pass

            time.sleep(5)

    poll_thread = threading.Thread(target=poll_review_notifications, daemon=True)
    poll_thread.start()

    # T-055: 启动任务通知轮询线程
    task_notify_thread = threading.Thread(target=poll_task_notifications, daemon=True)
    task_notify_thread.start()

    cli.start()


if __name__ == "__main__":
    main()
