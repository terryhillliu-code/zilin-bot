"""
知微 - OpenClaw Agent 飞书机器人
完整能力：AI对话 | 视频分析 | 语音转文字 | 图片理解 | 模型切换
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

# 应用凭证
APP_ID = "cli_a9142bd071bd1bd9"
APP_SECRET = "mlIZdNRvxpaVQIB6VQxHIee6WgW4UcPf"

# 消息去重
processed_messages = set()

# 创建 API 客户端
client = lark.Client.builder() \
    .app_id(APP_ID) \
    .app_secret(APP_SECRET) \
    .build()

# ========== 基础工具 ==========

def reply_message(message_id: str, text: str):
    """回复消息"""
    try:
        if len(text) > 4000:
            text = text[:3900] + "\n\n...(内容过长已截断)"
        content = json.dumps({"text": text})
        request = ReplyMessageRequest.builder() \
            .message_id(message_id) \
            .request_body(ReplyMessageRequestBody.builder()
                .content(content)
                .msg_type("text")
                .build()) \
            .build()
        response = client.im.v1.message.reply(request)
        if response.success():
            print(f"✅ 回复成功 ({len(text)} 字符)")
        else:
            print(f"❌ 回复失败: {response.code} - {response.msg}")
    except Exception as e:
        print(f"❌ 回复异常: {e}")

def get_session_id(user_id: str) -> str:
    return f"feishu-{user_id}"

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
            return f"❌ Agent 调用失败:\n{error[:500]}"
    except subprocess.TimeoutExpired:
        return "❌ 响应超时（5分钟），请稍后重试"
    except Exception as e:
        return f"❌ 调用异常: {str(e)}"

# ========== 语音处理 ==========

def download_audio(message_id: str, file_key: str) -> str:
    """下载飞书语音文件"""
    try:
        from lark_oapi.api.im.v1 import GetMessageResourceRequest
        tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".opus")
        tmp_path = tmp_file.name
        tmp_file.close()
        
        request = GetMessageResourceRequest.builder() \
            .message_id(message_id) \
            .file_key(file_key) \
            .type("file") \
            .build()
        response = client.im.v1.message_resource.get(request)
        
        if response.success():
            with open(tmp_path, "wb") as f:
                f.write(response.file.read())
            size = os.path.getsize(tmp_path)
            print(f"✅ 语音下载成功: {tmp_path} ({size} bytes)")
            return tmp_path
        else:
            print(f"❌ 语音下载失败: {response.code} - {response.msg}")
            return None
    except Exception as e:
        print(f"❌ 语音下载异常: {e}")
        return None

def transcribe_audio(audio_path: str) -> str:
    """转录语音：复制到容器 -> ffmpeg转格式 -> faster-whisper转录"""
    container_audio = None
    try:
        print(f"🎤 开始转录语音: {audio_path}")
        
        container_audio = f"/tmp/feishu_audio_{os.path.basename(audio_path)}"
        subprocess.run([
            "/usr/local/bin/docker", "cp",
            audio_path, f"clawdbot:{container_audio}"
        ], check=True, timeout=10)
        
        print("🎤 容器内转录中...")
        result = subprocess.run([
            "/usr/local/bin/docker", "exec", "clawdbot",
            "python3", "/root/workspace/scripts/transcribe.py", container_audio
        ], capture_output=True, text=True, timeout=120)
        
        if result.returncode == 0:
            text = result.stdout.strip()
            print(f"✅ 转录完成: {len(text)} 字符 - {text[:50]}...")
            return text
        else:
            error = result.stderr[:300] if result.stderr else "未知错误"
            print(f"❌ 转录失败: {error}")
            return None
    except subprocess.TimeoutExpired:
        print("❌ 转录超时（120秒）")
        return None
    except Exception as e:
        print(f"❌ 转录异常: {e}")
        return None
    finally:
        if audio_path and os.path.exists(audio_path):
            os.remove(audio_path)
        if container_audio:
            subprocess.run([
                "/usr/local/bin/docker", "exec", "clawdbot", "rm", "-f", container_audio
            ], capture_output=True)

def handle_audio_async(message_id: str, file_key: str, user_id: str):
    """异步处理语音消息（在线程中运行）"""
    try:
        audio_path = download_audio(message_id, file_key)
        if not audio_path:
            reply_message(message_id, "❌ 语音下载失败，请重试")
            return
        
        text = transcribe_audio(audio_path)
        if not text:
            reply_message(message_id, "❌ 语音转录失败，请重试")
            return
        
        session_id = get_session_id(user_id)
        response = call_openclaw_agent(f"[语音转文字] {text}", session_id)
        reply_message(message_id, response)
    except Exception as e:
        print(f"❌ 语音处理异常: {e}")
        reply_message(message_id, f"❌ 语音处理异常: {str(e)}")

# ========== 图片处理 ==========

def download_image(message_id: str, image_key: str) -> str:
    """下载飞书图片"""
    try:
        from lark_oapi.api.im.v1 import GetMessageResourceRequest
        tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
        tmp_path = tmp_file.name
        tmp_file.close()
        
        request = GetMessageResourceRequest.builder() \
            .message_id(message_id) \
            .file_key(image_key) \
            .type("image") \
            .build()
        response = client.im.v1.message_resource.get(request)
        
        if response.success():
            with open(tmp_path, "wb") as f:
                f.write(response.file.read())
            size = os.path.getsize(tmp_path)
            print(f"✅ 图片下载成功: {tmp_path} ({size} bytes)")
            return tmp_path
        else:
            print(f"❌ 图片下载失败: {response.code} - {response.msg}")
            return None
    except Exception as e:
        print(f"❌ 图片下载异常: {e}")
        return None

def analyze_image(image_path: str) -> str:
    """调用 qwen3.5-plus 分析图片"""
    try:
        import httpx
        
        with open(image_path, "rb") as f:
            image_data = base64.b64encode(f.read()).decode()
        
        env_path = os.path.expanduser("~/tanwei-bot/.env")
        api_key = None
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    if line.startswith("CODING_PLAN_API_KEY="):
                        api_key = line.split("=", 1)[1].strip().strip('"\'')
                        break
        
        if not api_key:
            return "❌ 未找到 API Key"
        
        print("🖼️ 调用 qwen3.5-plus 分析图片...")
        response = httpx.post(
            "https://coding.dashscope.aliyuncs.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            },
            json={
                "model": "qwen3.5-plus",
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "请分析这张图片，描述内容并提取关键信息。"},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_data}"}}
                    ]
                }],
                "max_tokens": 1000
            },
            timeout=60
        )
        
        if response.status_code == 200:
            result = response.json()["choices"][0]["message"]["content"]
            print(f"✅ 图片分析完成: {len(result)} 字符")
            return f"🖼️ 图片分析\n\n{result}"
        else:
            print(f"❌ 图片分析失败: {response.status_code} - {response.text[:200]}")
            return f"❌ 图片分析失败: {response.status_code}"
    except Exception as e:
        print(f"❌ 图片分析异常: {e}")
        return f"❌ 图片分析异常: {str(e)}"
    finally:
        if os.path.exists(image_path):
            os.remove(image_path)

def handle_image_async(message_id: str, image_key: str, user_id: str):
    """异步处理图片消息（在线程中运行）"""
    try:
        image_path = download_image(message_id, image_key)
        if not image_path:
            reply_message(message_id, "❌ 图片下载失败，请重试")
            return
        
        response = analyze_image(image_path)
        reply_message(message_id, response)
    except Exception as e:
        print(f"❌ 图片处理异常: {e}")
        reply_message(message_id, f"❌ 图片处理异常: {str(e)}")

# ========== 视频链接 ==========

def extract_video_url(text: str) -> str:
    patterns = [
        r'(https?://v\.douyin\.com/[A-Za-z0-9]+/?)',
        r'(https?://www\.douyin\.com/video/\d+)',
        r'(https?://(?:www\.)?youtube\.com/watch\?v=[A-Za-z0-9_-]+)',
        r'(https?://youtu\.be/[A-Za-z0-9_-]+)',
        r'(https?://(?:www\.)?bilibili\.com/video/[A-Za-z0-9]+)',
        r'(https?://b23\.tv/[A-Za-z0-9]+)'
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None

def is_video_url(text: str) -> bool:
    return extract_video_url(text) is not None

def process_video(text: str) -> str:
    try:
        url = extract_video_url(text)
        if not url:
            return "❌ 未找到有效的视频链接"
        print(f"🎬 视频链接: {url}")
        cmd = [
            "/usr/local/bin/docker", "exec", "clawdbot",
            "python3", "/root/workspace/skills/douyin-video-insight/insight.py",
            "--url", url, "--whisper-model", "small"
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            return f"❌ 视频分析失败\n\n{(result.stderr or result.stdout)[:500]}"
        output = result.stdout
        timestamp_match = re.search(r'(\d{8}_\d{6})', output)
        if timestamp_match:
            timestamp = timestamp_match.group(1)
        else:
            find_result = subprocess.run(
                ["/usr/local/bin/docker", "exec", "clawdbot", "ls", "-t", "/root/workspace/data/douyin_cache"],
                capture_output=True, text=True
            )
            dirs = find_result.stdout.strip().split('\n')
            timestamp = dirs[0] if dirs else None
        if not timestamp:
            return "❌ 未找到分析报告"
        cat_result = subprocess.run(
            ["/usr/local/bin/docker", "exec", "clawdbot", "cat",
             f"/root/workspace/data/douyin_cache/{timestamp}/report.md"],
            capture_output=True, text=True
        )
        if cat_result.returncode != 0:
            return "❌ 无法读取报告"
        report = cat_result.stdout
        if len(report) > 3800:
            report = report[:3700] + "\n\n...(内容过长已截断)"
        return f"🎬 视频分析报告\n\n{report}"
    except subprocess.TimeoutExpired:
        return "❌ 视频分析超时（10分钟）"
    except Exception as e:
        return f"❌ 视频处理异常: {str(e)}"

# ========== 文本命令 ==========

def show_help() -> str:
    return """🤖 知微 - OpenClaw Agent

【支持的消息类型】
✅ 文字 - AI 多轮对话
✅ 语音 - 自动转文字后对话
✅ 图片 - AI 图片理解分析
✅ 视频链接 - 下载并分析

【命令】
• /help - 显示帮助
• /reset - 重置对话
• /sync - 查看会话信息
• m1-m8 - 切换模型

【模型】
1-Qwen3.5  2-Coder  3-Max  4-Kimi
5-GLM5  6-MiniMax  7-Plus  8-Max按量

💡 直接发消息开始对话！"""

def handle_text_async(text: str, user_id: str, message_id: str):
    """异步处理文本消息（在线程中运行）"""
    try:
        text_lower = text.strip().lower()
        
        # 视频链接
        if is_video_url(text):
            reply_message(message_id, "🎬 正在分析视频，请稍候...")
            response = process_video(text)
            reply_message(message_id, response)
            return
        
        # 命令
        if text_lower in ["/help", "帮助", "/帮助"]:
            reply_message(message_id, show_help())
            return
        
        if text_lower in ["/reset", "重置", "/重置", "新对话"]:
            session_id = get_session_id(user_id)
            reply_message(message_id, f"✅ 会话已重置\n\n新会话 ID: {session_id}")
            return
        
        if text_lower in ["/sync", "同步", "/session", "会话"]:
            session_id = get_session_id(user_id)
            reply_message(message_id, f"📌 会话 ID: {session_id}")
            return
        
        # 模型切换
        if len(text_lower) == 2 and text_lower[0] == 'm' and text_lower[1] in "12345678":
            try:
                result = subprocess.run(
                    ['/usr/local/bin/ocmodel', text_lower[1]],
                    capture_output=True, text=True, timeout=5
                )
                msg = f"✅ {result.stdout.strip()}" if result.returncode == 0 else f"❌ {result.stderr}"
                reply_message(message_id, msg)
            except Exception as e:
                reply_message(message_id, f"❌ 切换异常: {e}")
            return
        
        # Agent 对话
        session_id = get_session_id(user_id)
        response = call_openclaw_agent(text, session_id)
        reply_message(message_id, response)
    except Exception as e:
        print(f"❌ 文本处理异常: {e}")
        reply_message(message_id, f"❌ 处理异常: {str(e)}")

# ========== 消息分发 ==========

def do_p2_im_message_receive_v1(data) -> None:
    """处理收到的消息（快速返回，重活交给线程）"""
    global processed_messages
    
    try:
        event = data.event
        message = event.message
        message_id = message.message_id
        
        # 去重
        if message_id in processed_messages:
            return
        processed_messages.add(message_id)
        if len(processed_messages) > 1000:
            processed_messages = set(list(processed_messages)[-500:])
        
        msg_type = message.message_type
        content_str = message.content
        
        # 获取发送者
        sender = event.sender
        user_id = "unknown"
        if sender and sender.sender_id:
            user_id = sender.sender_id.user_id or sender.sender_id.open_id or sender.sender_id.union_id or "unknown"
        
        print(f"\n{'='*50}")
        print(f"📨 [{msg_type}] 用户: {str(user_id)[:10]}...")
        
        content_dict = json.loads(content_str)
        
        if msg_type == "text":
            text = content_dict.get("text", "")
            text = re.sub(r'@_user_\d+\s*', '', text).strip()
            print(f"   文本: {text[:50]}...")
            if text:
                thread = threading.Thread(target=handle_text_async, args=(text, user_id, message_id))
                thread.start()
        
        elif msg_type == "audio":
            file_key = content_dict.get("file_key", "")
            print(f"   语音: {file_key[:30]}...")
            reply_message(message_id, "🎤 正在转录语音，请稍候...")
            thread = threading.Thread(target=handle_audio_async, args=(message_id, file_key, user_id))
            thread.start()
        
        elif msg_type == "image":
            image_key = content_dict.get("image_key", "")
            print(f"   图片: {image_key[:30]}...")
            reply_message(message_id, "🖼️ 正在分析图片，请稍候...")
            thread = threading.Thread(target=handle_image_async, args=(message_id, image_key, user_id))
            thread.start()
        
        elif msg_type in ["media", "file"]:
            reply_message(message_id, "📁 暂不支持该文件类型\n\n支持：文字 | 语音 | 图片 | 视频链接")
        
        else:
            print(f"   ⏭️ 不支持: {msg_type}")
        
        print(f"{'='*50}")
        
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
    
    print("🤖 知微 Agent 启动")
    print("   支持: 文字 | 语音 | 图片 | 视频链接")
    print("   命令: /help | /reset | m1-m8")
    print("-" * 50)
    
    cli.start()

if __name__ == "__main__":
    main()
