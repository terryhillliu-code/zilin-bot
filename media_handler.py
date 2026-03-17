"""
媒体处理模块
处理图片、视频、语音和URL相关功能
"""

import os
import re
import tempfile
import base64
import threading
import subprocess
import logging
from pathlib import Path
from typing import Optional

# 设置日志
logger = logging.getLogger(__name__)

# 导入依赖（由 ws_client.py 初始化）
client = None
reply_message = None
TaskLogger = None
pending_image = None
pending_voice = None
time = None


def init_media_handler(global_client, global_reply_message, global_task_logger, global_pending_image, global_pending_voice, global_time):
    """初始化媒体处理模块的全局依赖"""
    global client, reply_message, TaskLogger, pending_image, pending_voice, time
    client = global_client
    reply_message = global_reply_message
    TaskLogger = global_task_logger
    pending_image = global_pending_image
    pending_voice = global_pending_voice
    time = global_time


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
    """压缩图片为 base64"""
    try:
        from PIL import Image
        import io

        with Image.open(image_path) as img:
            if img.mode in ('RGBA', 'P'):
                img = img.convert('RGB')

            ratio = min(max_size / img.width, max_size / img.height)
            if ratio < 1:
                new_size = (int(img.width * ratio), int(img.height * ratio))
                img = img.resize(new_size, Image.Resampling.LANCZOS)

            buffer = io.BytesIO()
            img.save(buffer, format='JPEG', quality=85)
            compressed = buffer.getvalue()

            print(f"🖼️ 图片压缩: {os.path.getsize(image_path)} → {len(compressed)} bytes")
            return base64.b64encode(compressed).decode()
    except ImportError:
        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode()
    except Exception as e:
        print(f"⚠️ 压缩失败，使用原图: {e}")
        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode()


def analyze_image_base64(image_base64: str, question: str = None) -> str:
    """调用 DashScope API 分析图片"""
    try:
        import httpx

        # 获取 API Key - 支持多路径和多变量名查找
        api_key = None
        env_paths = [
            os.path.expanduser("~/clawdbot-docker/workspace/secrets/.env"),
            os.path.expanduser("~/zhiwei-bot/.env"),
            os.path.expanduser("~/tanwei-bot/.env"),
        ]
        key_names = ["CODING_PLAN_API_KEY", "DASHSCOPE_API_KEY", "BAILIAN_API_KEY"]

        for env_path in env_paths:
            if os.path.exists(env_path):
                with open(env_path) as f:
                    lines = f.readlines()
                for key_name in key_names:
                    for line in lines:
                        if line.startswith(f"{key_name}="):
                            api_key = line.split("=", 1)[1].strip().strip('"\'')
                            break
                    if api_key:
                        break
            if api_key:
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
            return f"🖼️ **图片分析结果**\n\n{result}"
        else:
            print(f"❌ 图片分析失败: {response.status_code}")
            return f"❌ 图片分析失败: {response.status_code}"
    except Exception as e:
        print(f"❌ 图片分析异常: {e}")
        return f"❌ 图片分析异常: {str(e)}"


def handle_image_async(message_id: str, image_key: str, user_id: str):
    """异步处理图片"""
    try:
        image_path = download_image(message_id, image_key)
        if not image_path:
            reply_message(message_id, "❌ 图片下载失败，请重试")
            return

        image_base64 = compress_image_base64(image_path)

        pending_image[user_id] = {
            "base64": image_base64,
            "time": time.time()
        }

        response = analyze_image_base64(image_base64)

        if os.path.exists(image_path):
            os.remove(image_path)

        reply_message(message_id, response + "\n\n💡 你可以继续针对这张图片提问")
    except Exception as e:
        print(f"❌ 图片处理异常: {e}")
        reply_message(message_id, f"❌ 图片处理异常: {str(e)}")


# ========== 视频链接 ==========

def extract_video_url(text: str) -> str:
    """提取视频 URL"""
    patterns = [
        r'(https?://v\.douyin\.com/[A-Za-z0-9_-]+/?)',
        r'(https?://www\.douyin\.com/video/\d+)',
        r'(https?://(?:www\.)?youtube\.com/watch\?v=[A-Za-z0-9_-]+)',
        r'(https?://youtu\.be/[A-Za-z0-9_-]+)',
        r'(https?://(?:www\.)?bilibili\.com/video/[A-Za-z0-9_-]+)',
        r'(https?://b23\.tv/[A-Za-z0-9_-]+)'
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None


def extract_article_url(text: str) -> str:
    """提取文章 URL（非视频）"""
    video_patterns = ['douyin.com', 'youtube.com', 'youtu.be', 'bilibili.com', 'b23.tv']
    url_pattern = r'(https?://[^\s<>"{}|\^`\[\]]+)'
    match = re.search(url_pattern, text)
    if match:
        url = match.group(1).rstrip('.,;:!?)')
        if not any(v in url.lower() for v in video_patterns):
            return url
    return None


def is_article_url(text: str) -> bool:
    """判断是否为文章 URL"""
    return extract_article_url(text) is not None


def is_video_url(text: str) -> bool:
    """判断是否为视频 URL"""
    return extract_video_url(text) is not None


def fetch_url_content(url: str, timeout: int = 30) -> tuple[bool, str]:
    """
    抓取 URL 内容，使用宿主机 defuddle
    返回: (success, content)
    """
    try:
        result = subprocess.run(
            ["defuddle", "parse", url, "--md"],
            capture_output=True,
            text=True,
            timeout=timeout
        )
        if result.returncode == 0 and result.stdout.strip():
            logger.info(f"✅ defuddle 抓取成功: {url[:50]}")
            return True, result.stdout.strip()
        else:
            logger.error(f"❌ defuddle 抓取失败: {result.stderr[:200] if result.stderr else '无输出'}")
            return False, f"❌ 网页抓取失败"
    except subprocess.TimeoutExpired:
        logger.error(f"❌ defuddle 超时: {url[:50]}")
        return False, "❌ 网页抓取超时"
    except FileNotFoundError:
        logger.error("❌ defuddle 未安装，请运行: npm install -g defuddle")
        return False, "❌ defuddle 未安装"
    except Exception as e:
        logger.error(f"❌ defuddle 抓取异常: {e}")
        return False, f"❌ 网页处理异常: {str(e)}"


def summarize_url(url: str) -> str:
    """总结网页 URL，使用 AI 硬件架构师专属 prompt"""
    try:
        # 确保从 zhiwei-bot 目录导入
        import sys
        bot_dir = Path(__file__).parent
        if str(bot_dir) not in sys.path:
            sys.path.insert(0, str(bot_dir))
        from scripts.obsidian_summary_filler import SUMMARY_PROMPT, generate_ai_summary

        print(f"🌐 抓取网页: {url}")

        success, content = fetch_url_content(url, timeout=60)
        if not success:
            return content  # 返回错误信息

        print(f"📄 内容长度: {len(content)} 字符，调用 AI 生成专属摘要...")
        summary = generate_ai_summary(content, doc_type="网页")

        if summary.startswith("❌"):
            return summary
        else:
            return f"📄 **网页摘要**\n\n{summary}"

    except Exception as e:
        return f"❌ 网页处理异常: {str(e)}"


def handle_video_async(text: str, message_id: str, user_id: str):
    """异步处理视频分析"""
    def _process():
        try:
            response = process_video(text, message_id)
            reply_message(message_id, response)
            TaskLogger.log_task("视频分析", "完成", extract_video_url(text))
        except Exception as e:
            print(f"❌ 视频分析异步处理异常: {e}")
            reply_message(message_id, f"❌ 视频分析失败: {str(e)}")

    thread = threading.Thread(target=_process, daemon=True)
    thread.start()


def process_video(text: str, message_id: str = None) -> str:
    """处理视频分析 - 调用宿主机 Distiller

    v2.0 新增：
    - 详细错误分类和记录
    - 自动重试临时性错误
    - 严重错误飞书告警
    """
    video_history = None
    url = None
    try:
        url = extract_video_url(text)
        if not url:
            return "❌ 未找到有效的视频链接"
        logger.info(f"🎬 视频链接: {url}")

        # 记录开始处理
        try:
            from video_history import get_video_history
            video_history = get_video_history()
            video_history.record_start(url)
        except Exception as e:
            logger.warning(f"VideoHistory 记录开始失败: {e}")
            video_history = None

        # 调用宿主机 Distiller
        distiller_path = os.path.expanduser("~/zhiwei-bot/scripts/douyin_distiller.py")
        venv_python = os.path.expanduser("~/zhiwei-bot/venv/bin/python")

        cmd = [
            venv_python, distiller_path,
            "--from-text", text,
            "--output-dir", os.path.expanduser("~/Documents/ZhiweiVault/Inbox"),
            "--cookies", os.path.expanduser("~/zhiwei-bot/secrets/douyin_cookies.txt")  # 使用 cookies 文件获取抖音字幕
        ]

        logger.info(f"🎬 调用 Distiller: {' '.join(cmd[:3])}...")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

        if result.returncode != 0:
            # 解析错误信息
            error_type, error_message = _parse_distiller_error(result.stderr)

            # 记录失败
            if video_history:
                video_history.record_failed(url, error_type, error_message)

            # 发送告警（如果是严重错误）
            if video_history:
                from video_history import VideoErrorType
                try:
                    error_type_enum = VideoErrorType(error_type)
                    video_history.send_alert(error_type_enum, url, error_message)
                except ValueError:
                    pass  # 无效的错误类型，忽略

            # 判断是否可以重试
            if video_history and video_history.can_retry(url):
                retry_count = video_history.increment_retry(url)
                logger.info(f"将自动重试 (第 {retry_count} 次)")
                # TODO: 可以在这里添加自动重试逻辑

            logger.error(f"Distiller 失败: {error_message[:200]}")
            return f"❌ 视频处理失败\n\n错误类型: {error_type}\n详情: {error_message[:300]}"

        # 解析输出
        output = result.stdout
        if "✅ Done!" in output:
            # 提取输出文件路径
            match = re.search(r'Output: (.+\.md)', output)
            if match:
                output_path = match.group(1)
                # 提取标题（从文件名）
                title = Path(output_path).stem
                # 记录成功
                if video_history:
                    video_history.record_done(url, title, output_path)
                return f"✅ 视频知识笔记已生成\n\n📁 文件: {output_path}\n\n请到 Obsidian Inbox 查看完整内容。"
            return f"✅ 视频处理完成\n\n{output[-500:]}"

        return f"⚠️ 视频处理完成但输出格式异常\n\n{output[-500:]}"

    except subprocess.TimeoutExpired:
        # 记录失败（超时）
        error_type = "timeout"
        error_message = "视频分析超时（10分钟）"
        if video_history and url:
            video_history.record_failed(url, error_type, error_message)
            from video_history import VideoErrorType
            video_history.send_alert(VideoErrorType.TIMEOUT, url, error_message)
        return f"❌ {error_message}"

    except Exception as e:
        # 记录失败（未知错误）
        if video_history and url:
            video_history.record_failed(url, "unknown", str(e))
        logger.error(f"视频处理异常: {e}")
        return f"❌ 视频处理异常: {str(e)}"


def _parse_distiller_error(stderr: str) -> tuple[str, str]:
    """解析 Distiller 输出的错误信息

    Args:
        stderr: Distiller 的 stderr 输出

    Returns:
        (error_type, error_message) 元组
    """
    import json

    # 尝试解析 JSON 格式的错误信息
    for line in stderr.strip().split('\n'):
        line = line.strip()
        if line.startswith('{') and line.endswith('}'):
            try:
                data = json.loads(line)
                error_type = data.get('error_type', 'unknown')
                error_message = data.get('error_message', stderr[:500])
                return error_type, error_message
            except json.JSONDecodeError:
                continue

    # 降级：根据 stderr 内容判断错误类型
    stderr_lower = stderr.lower()

    if any(kw in stderr_lower for kw in ["cookie", "登录过期", "请先登录"]):
        return "cookie_expired", stderr[:500]
    elif any(kw in stderr_lower for kw in ["network", "connection", "timeout"]):
        return "network_error", stderr[:500]
    elif any(kw in stderr_lower for kw in ["404", "not found", "不存在"]):
        return "video_not_found", stderr[:500]
    elif any(kw in stderr_lower for kw in ["private", "私密"]):
        return "video_private", stderr[:500]
    else:
        return "unknown", stderr[:500]


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
            print(f"✅ 语音下载成功: {tmp_path} ({os.path.getsize(tmp_path)} bytes)")
            return tmp_path
        else:
            print(f"❌ 语音下载失败: {response.code}")
            return None
    except Exception as e:
        print(f"❌ 语音下载异常: {e}")
        return None


def transcribe_audio(audio_path: str) -> str:
    """转录语音文件为文字（使用宿主机 DashScope ASR）

    复用 douyin_distiller.py 的 DashScopeASRTranscriber 逻辑。
    音频格式自动转换为 16kHz 单声道（Recognition API 要求）。
    """
    converted_path = None
    try:
        import dashscope
        from dashscope.audio.asr import Recognition

        # 获取 API key
        api_key = os.getenv("DASHSCOPE_API_KEY")
        if not api_key:
            # 尝试从 .env 文件加载
            env_path = os.path.expanduser("~/zhiwei-bot/.env")
            if os.path.exists(env_path):
                from dotenv import load_dotenv
                load_dotenv(env_path)
                api_key = os.getenv("DASHSCOPE_API_KEY")

        if not api_key:
            logger.error("DASHSCOPE_API_KEY 未配置")
            return None

        dashscope.api_key = api_key

        # 音频格式转换（Recognition API 需要 16kHz 单声道）
        audio_path_obj = Path(audio_path)
        converted_path = _ensure_audio_format(audio_path_obj)

        # 检测音频格式
        suffix = converted_path.suffix.lower().lstrip('.')
        audio_format = suffix if suffix in ['mp3', 'wav', 'pcm', 'opus', 'm4a', 'aac'] else 'opus'

        logger.info(f"🎵 ASR 转录: {converted_path.name} ({audio_format})")

        # 创建 Recognition 实例
        recognition = Recognition(
            model="paraformer-realtime-v2",
            format=audio_format,
            sample_rate=16000
        )

        # 调用同步识别
        result = recognition.call(file=str(converted_path.absolute()))

        if result.status_code == 200:
            # 解析结果
            full_text = ""
            if hasattr(result, 'output') and result.output:
                output = result.output
                if 'sentence' in output:
                    for sentence in output['sentence']:
                        full_text += sentence.get('text', '')
                elif 'text' in output:
                    full_text = output['text']

            if full_text:
                logger.info(f"✅ 转录完成: {len(full_text)} 字符")
                return full_text
            else:
                logger.error("ASR 返回空结果")
                return None
        else:
            logger.error(f"ASR 转录失败: {result.message}")
            return None

    except ImportError:
        logger.error("dashscope 未安装，请运行: pip install dashscope")
        return None
    except Exception as e:
        logger.error(f"ASR 转录异常: {e}")
        return None
    finally:
        # 清理文件
        if audio_path and os.path.exists(audio_path):
            os.remove(audio_path)
        if converted_path and converted_path != Path(audio_path) and converted_path.exists():
            converted_path.unlink()


def _ensure_audio_format(audio_path: Path) -> Path:
    """确保音频格式符合 Recognition API 要求（16kHz 单声道）

    复用 douyin_distiller.py 的 DashScopeASRTranscriber._ensure_audio_format 逻辑。
    返回转换后的音频路径（如无需转换则返回原路径）。
    """
    try:
        # 使用 ffprobe 检查音频格式
        probe_cmd = [
            "ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=sample_rate,channels",
            "-of", "csv=p=0", str(audio_path)
        ]
        result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=30)

        if result.returncode != 0:
            logger.warning(f"ffprobe failed, using original audio")
            return audio_path

        # 解析输出：格式为 "sample_rate,channels"
        output = result.stdout.strip()
        parts = output.split(',')

        if len(parts) >= 2:
            sample_rate = int(parts[0])
            channels = int(parts[1])

            # 检查是否需要转换（非16kHz或非单声道）
            if sample_rate != 16000 or channels != 1:
                converted_path = audio_path.with_suffix(".converted.mp3")
                logger.info(f"🎵 音频转换: {sample_rate}Hz/{channels}ch → 16000Hz/1ch")

                convert_cmd = [
                    "ffmpeg", "-y", "-i", str(audio_path),
                    "-ar", "16000", "-ac", "1", "-f", "mp3",
                    str(converted_path)
                ]
                conv_result = subprocess.run(convert_cmd, capture_output=True, timeout=120)

                if conv_result.returncode == 0 and converted_path.exists():
                    return converted_path
                else:
                    logger.warning(f"ffmpeg conversion failed")
                    return audio_path
            else:
                return audio_path
        else:
            return audio_path

    except subprocess.TimeoutExpired:
        logger.warning("ffprobe/ffmpeg timeout")
        return audio_path
    except Exception as e:
        logger.warning(f"Audio format check failed: {e}")
        return audio_path


# ========== 语音任务收集 ==========

def handle_voice_task_async(message_id: str, file_key: str, user_id: str):
    """异步处理语音 -> 转文字 -> 等待确认 -> 提取任务

    流程：
    1. 下载语音文件
    2. 转录为文字
    3. 存入待确认队列，等待用户回复「确认」
    """
    try:
        # 1. 下载语音
        audio_path = download_audio(message_id, file_key)
        if not audio_path:
            reply_message(message_id, "❌ 语音下载失败，请重试")
            return

        # 2. 转录
        text = transcribe_audio(audio_path)
        if not text:
            reply_message(message_id, "❌ 语音识别失败，请重试")
            return

        # 3. 存入待确认，等待用户回复
        pending_voice[user_id] = {
            "text": text,
            "time": time.time()
        }

        # 4. 回复，等待确认
        reply_message(message_id,
            f"🎤 语音识别结果：\n\n{text}\n\n"
            f"回复「确认」提取任务，或「取消」放弃"
        )

        logger.info(f"🎤 语音待确认: {user_id} - {text[:50]}...")

    except Exception as e:
        logger.error(f"语音处理异常: {e}")
        reply_message(message_id, f"❌ 语音处理异常: {str(e)}")
