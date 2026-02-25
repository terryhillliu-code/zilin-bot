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

# 简单限流
import time
from collections import defaultdict

user_last_request = defaultdict(float)
RATE_LIMIT_SECONDS = 2

# 待确认的语音转录（user_id -> text）
pending_voice = {}

# 待处理的图片（user_id -> image_path）
pending_image = {}  # user_id -> {'base64': data, 'path': path}

# 定期清理过期图片（超过10分钟）
def cleanup_pending_images():
    """清理过期的待处理图片（10分钟过期）"""
    global pending_image
    import time
    current_time = time.time()
    expired = []
    for user_id, data in pending_image.items():
        if isinstance(data, dict):
            # 新格式：{'base64': ..., 'time': ...}
            if current_time - data.get("time", 0) > 600:
                expired.append(user_id)
        else:
            # 旧格式：路径字符串，直接清理
            expired.append(user_id)
    for user_id in expired:
        del pending_image[user_id]



  # 每用户最少间隔2秒

# 对话历史（user_id -> [(time, role, text), ...]）
from collections import deque
chat_history = {}
MAX_HISTORY = 20

def add_to_history(user_id: str, role: str, text: str):
    """添加到对话历史"""
    import time
    if user_id not in chat_history:
        chat_history[user_id] = deque(maxlen=MAX_HISTORY)
    chat_history[user_id].append((time.strftime("%H:%M"), role, text[:100]))

def get_history(user_id: str) -> str:
    """获取对话历史"""
    if user_id not in chat_history or not chat_history[user_id]:
        return "📜 暂无对话记录"
    
    lines = ["📜 最近对话记录\n"]
    for t, role, text in chat_history[user_id]:
        icon = "👤" if role == "user" else "🤖"
        lines.append(f"{t} {icon} {text}...")
    return "\n".join(lines)


def get_quick_status() -> str:
    """快速获取系统状态"""
    import subprocess
    lines = ["📊 系统状态\n"]
    
    # Docker 容器
    try:
        result = subprocess.run(
            "docker ps --format '{{.Names}}: {{.Status}}' | head -3",
            shell=True, capture_output=True, text=True, timeout=5
        )
        lines.append("**容器:**")
        for line in result.stdout.strip().split('\n')[:3]:
            if line:
                lines.append(f"  • {line}")
    except:
        lines.append("  • 容器状态获取失败")
    
    # 当前模型
    try:
        import json
        config_path = os.path.expanduser("~/logs/current_model.json")
        if os.path.exists(config_path):
            with open(config_path) as f:
                data = json.load(f)
                lines.append(f"\n**模型:** {data.get('name', '未知')}")
    except:
        pass
    
    # 磁盘
    try:
        result = subprocess.run(
            "df -h / | tail -1 | awk '{print $4}'",
            shell=True, capture_output=True, text=True, timeout=5
        )
        lines.append(f"**磁盘剩余:** {result.stdout.strip()}")
    except:
        pass
    
    return "\n".join(lines)


def check_rate_limit(user_id: str) -> bool:
    """检查是否触发限流，返回 True 表示允许"""
    now = time.time()
    last = user_last_request[user_id]
    if now - last < RATE_LIMIT_SECONDS:
        return False
    user_last_request[user_id] = now
    return True



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


def reply_card(message_id: str, title: str, content_text: str):
    """用飞书卡片回复（Markdown 格式）"""
    try:
        if len(content_text) > 3500:
            content_text = content_text[:3400] + "\n\n...(内容过长已截断)"
        
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "blue"
            },
            "elements": [
                {"tag": "markdown", "content": content_text}
            ]
        }
        
        request = ReplyMessageRequest.builder() \
            .message_id(message_id) \
            .request_body(ReplyMessageRequestBody.builder()
                .content(json.dumps(card))
                .msg_type("interactive")
                .build()) \
            .build()
        
        response = client.im.v1.message.reply(request)
        if response.success():
            print(f"✅ 卡片回复成功")
        else:
            # 卡片失败则回退到文本
            print(f"⚠️ 卡片失败，回退文本: {response.code}")
            reply_message(message_id, f"{title}\n\n{content_text}")
    except Exception as e:
        print(f"❌ 卡片异常: {e}")
        reply_message(message_id, f"{title}\n\n{content_text}")


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
            return "❌ AI 暂时无法响应，请稍后重试"
    except subprocess.TimeoutExpired:
        return "⏰ 响应超时，请简化问题后重试"
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
            "python3", "/root/workspace/scripts/transcribe_aliyun.py", container_audio
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
    """异步处理语音消息 - 确认模式"""
    global pending_voice
    try:
        audio_path = download_audio(message_id, file_key)
        if not audio_path:
            reply_message(message_id, "❌ 语音下载失败，请重试")
            return
        
        text = transcribe_audio(audio_path)
        if not text:
            reply_message(message_id, "❌ 语音转录失败，请重试")
            return
        
        # 保存待确认内容
        pending_voice[user_id] = text
        
        # 回显转录结果，等待确认
        confirm_msg = f"""🎤 语音转录结果：

「{text}」

请回复：
• **ok** 或 **确认** → 执行上述内容
• **直接输入修改后的文字** → 执行修改后的内容
• **取消** → 放弃本次语音"""
        
        reply_message(message_id, confirm_msg)
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



def compress_image_base64(image_path: str, max_size: int = 800) -> str:
    """压缩图片并返回 base64（减少 API 调用时间）"""
    try:
        from PIL import Image
        import io
        
        with Image.open(image_path) as img:
            # 转换为 RGB（去除 alpha 通道）
            if img.mode in ('RGBA', 'P'):
                img = img.convert('RGB')
            
            # 等比例缩放
            ratio = min(max_size / img.width, max_size / img.height)
            if ratio < 1:
                new_size = (int(img.width * ratio), int(img.height * ratio))
                img = img.resize(new_size, Image.Resampling.LANCZOS)
            
            # 压缩为 JPEG
            buffer = io.BytesIO()
            img.save(buffer, format='JPEG', quality=85)
            compressed = buffer.getvalue()
            
            print(f"🖼️ 图片压缩: {os.path.getsize(image_path)} → {len(compressed)} bytes")
            return base64.b64encode(compressed).decode()
    except ImportError:
        # 没有 PIL，直接读取原图
        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode()
    except Exception as e:
        print(f"⚠️ 压缩失败，使用原图: {e}")
        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode()

def analyze_image_base64(image_base64: str, question: str = None) -> str:
    """直接用 base64 分析图片"""
    try:
        import httpx
        
        env_path = os.path.expanduser("~/tanwei-bot/.env")
        api_key = None
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    if line.startswith("CODING_PLAN_API_KEY="):
                        api_key = line.split("=", 1)[1].strip().strip('"\'')
                        break
        
        if not api_key:
            return "❌ 系统配置异常，请联系管理员"
        
        prompt = question if question else "请分析这张图片，描述内容并提取关键信息。"
        
        print(f"🖼️ 调用 qwen3.5-plus... 问题: {prompt[:30]}...")
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
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}}
                    ]
                }],
                "max_tokens": 2000
            },
            timeout=90
        )
        
        if response.status_code == 200:
            result = response.json()["choices"][0]["message"]["content"]
            print(f"✅ 图片分析完成: {len(result)} 字符")
            return f"**图片分析结果**\n\n{result}"
        else:
            print(f"❌ 图片分析失败: {response.status_code}")
            return f"❌ 图片分析失败: {response.status_code}"
    except Exception as e:
        print(f"❌ 图片分析异常: {e}")
        return f"❌ 图片分析异常: {str(e)}"

def analyze_image(image_path: str, question: str = None) -> str:
    """调用 qwen3.5-plus 分析图片，支持自定义提问"""
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
            return "❌ 系统配置异常，请联系管理员"
        
        # 使用自定义问题或默认问题
        prompt = question if question else "请分析这张图片，描述内容并提取关键信息。"
        
        print(f"🖼️ 调用 qwen3.5-plus 分析图片... 问题: {prompt[:30]}...")
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
                        {"type": "text", "text": prompt},
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
            return f"**图片分析结果**\n\n{result}"
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
    """异步处理图片消息 - 支持后续追问"""
    global pending_image
    try:
        image_path = download_image(message_id, image_key)
        if not image_path:
            reply_message(message_id, "❌ 图片下载失败，请重试")
            return
        
        # 压缩并保存 base64（用于追问，减少 API 延迟）
        image_base64 = compress_image_base64(image_path)
        
        pending_image[user_id] = {
            "base64": image_base64,
            "time": __import__("time").time()
        }
        
        # 先做默认分析
        response = analyze_image_base64(image_base64)
        
        # 删除临时文件
        if os.path.exists(image_path):
            os.remove(image_path)
        
        reply_message(message_id, response + "\n\n💡 你可以继续针对这张图片提问")
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


def extract_article_url(text: str) -> str:
    """提取普通网页 URL（排除视频链接）"""
    # 排除视频平台
    video_patterns = ['douyin.com', 'youtube.com', 'youtu.be', 'bilibili.com', 'b23.tv']
    
    # 匹配 URL
    url_pattern = r'(https?://[^\s<>"{}|\^`\[\]]+)'
    match = re.search(url_pattern, text)
    if match:
        url = match.group(1).rstrip('.,;:!?)')
        # 排除视频链接
        if not any(v in url.lower() for v in video_patterns):
            return url
    return None

def is_article_url(text: str) -> bool:
    return extract_article_url(text) is not None

def summarize_url(url: str) -> str:
    """调用 web-summary skill 总结网页"""
    try:
        print(f"🌐 抓取网页: {url}")
        cmd = [
            "/usr/local/bin/docker", "exec", "clawdbot",
            "python3", "/root/workspace/skills/web-summary/websummary.py", "fetch", "--url", url
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        
        if result.returncode != 0:
            return f"❌ 网页抓取失败: {result.stderr[:200]}"
        
        content = result.stdout.strip()
        if len(content) < 100:
            return f"❌ 网页内容太少，无法总结"
        
        # 截取前8000字符避免超限
        if len(content) > 8000:
            content = content[:8000] + "..."
        
        print(f"📄 内容长度: {len(content)} 字符，调用 AI 总结...")
        
        # 调用 Agent 总结
        summary_prompt = f"请总结以下网页内容，提取关键信息：\n\n{content}"
        return summary_prompt  # 返回 prompt，由 handle_text_async 调用 Agent
        
    except subprocess.TimeoutExpired:
        return "❌ 网页抓取超时"
    except Exception as e:
        return f"❌ 网页处理异常: {str(e)}"

def is_video_url(text: str) -> bool:
    return extract_video_url(text) is not None

def process_video(text: str, message_id: str = None) -> str:
    try:
        url = extract_video_url(text)
        if not url:
            return "❌ 未找到有效的视频链接"
        print(f"🎬 视频链接: {url}")
        
        # 进度更新函数
        def update_progress(step, msg):
            if message_id:
                try:
                    reply_message(message_id, f"🎬 分析中...\n\n⏳ {msg}")
                except:
                    pass
        
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

【支持的消息】
📝 文字 - AI 多轮对话
🖼️ 图片 - 图片分析 + 追问
🌐 网页链接 - 自动抓取总结
🎬 视频链接 - 抖音/YouTube/B站

【命令】
/help - 显示帮助
/reset - 重置对话
/history - 查看对话记录
/sync - 查看会话ID
m1-m8 - 切换模型

【模型】
1-Qwen3.5  2-Coder  3-Max  4-Kimi
5-GLM5  6-MiniMax  7-Plus  8-Max

💡 直接发消息开始对话！"""

def handle_text_async(text: str, user_id: str, message_id: str):
    """异步处理文本消息（在线程中运行）"""
    global pending_voice
    try:
        text_lower = text.strip().lower()
        
        # 检查是否有待确认的语音
        if user_id in pending_voice:
            pending_text = pending_voice.pop(user_id)
            
            if text_lower in ["取消", "cancel", "算了"]:
                reply_message(message_id, "✅ 已取消")
                return
            
            if text_lower in ["ok", "确认", "好的", "执行", "是", "yes"]:
                # 执行原始转录内容
                session_id = get_session_id(user_id)
                response = call_openclaw_agent(pending_text, session_id)
                reply_message(message_id, response)
                return
            
            # 否则使用用户输入的修改内容
            session_id = get_session_id(user_id)
            response = call_openclaw_agent(text, session_id)
            reply_message(message_id, response)
            return
        
        # 检查是否有待处理的图片（图片追问）
        if user_id in pending_image:
            img_data = pending_image[user_id]
            if isinstance(img_data, dict) and "base64" in img_data:
                # 检查是否过期（10分钟）
                if __import__("time").time() - img_data.get("time", 0) < 600:
                    print(f"🖼️ 图片追问: {text[:30]}...")
                    response = analyze_image_base64(img_data["base64"], text)
                    reply_message(message_id, response + "\n\n💡 继续追问或发送新图片")
                    return
                else:
                    del pending_image[user_id]
            else:
                del pending_image[user_id]
        
        # 网页链接（非视频）
        if is_article_url(text):
            url = extract_article_url(text)
            reply_message(message_id, "🌐 正在抓取网页，请稍候...")
            summary_prompt = summarize_url(url)
            if summary_prompt.startswith("❌"):
                reply_message(message_id, summary_prompt)
                return
            session_id = get_session_id(user_id)
            response = call_openclaw_agent(summary_prompt, session_id)
            reply_card(message_id, "🌐 网页总结", response)
            return
        
        # 视频链接
        if is_video_url(text):
            reply_message(message_id, "🎬 开始分析视频...\n\n⏳ 步骤1/4: 下载视频")
            response = process_video(text, message_id)
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
        
        if text_lower in ["/history", "历史", "/记录"]:
            reply_message(message_id, get_history(user_id))
            return
        
        if text_lower in ["/status", "状态", "/状态"]:
            reply_message(message_id, get_quick_status())
            return
        
        if text_lower in ["/model", "模型", "/模型"]:
            try:
                import json
                config_path = os.path.expanduser("~/logs/current_model.json")
                if os.path.exists(config_path):
                    with open(config_path) as f:
                        data = json.load(f)
                    msg = f"""🤖 当前模型

**{data.get('name', '未知')}**
• 模型ID: {data.get('model', '未知')}
• Provider: {data.get('provider', '未知')}

切换命令: m1-m8
1-Qwen3.5  2-Coder  3-Max  4-Kimi
5-GLM5  6-MiniMax  7-Plus  8-Max"""
                    reply_message(message_id, msg)
                else:
                    reply_message(message_id, "❌ 模型配置未找到")
            except Exception as e:
                reply_message(message_id, f"❌ 获取模型失败: {e}")
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
        add_to_history(user_id, "user", text)
        session_id = get_session_id(user_id)
        response = call_openclaw_agent(text, session_id)
        add_to_history(user_id, "bot", response)
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
        
        # 清理过期图片
        cleanup_pending_images()
        
        # 去重
        if message_id in processed_messages:
            return
        processed_messages.add(message_id)
        
        # 限流检查
        sender = event.sender
        temp_user_id = "unknown"
        if sender and sender.sender_id:
            temp_user_id = sender.sender_id.user_id or sender.sender_id.open_id or "unknown"
        if not check_rate_limit(temp_user_id):
            print(f"⚠️ 限流: {temp_user_id}")
            return
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
            print(f"   语音: 暂不支持")
            reply_message(message_id, "🎤 语音识别功能暂时关闭\n\n请直接发送文字消息，或使用飞书自带的语音转文字功能后发送")
        
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
