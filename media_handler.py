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
import sys
from pathlib import Path
from typing import Optional

# 导入统一的 API Key 获取函数
try:
    from zhiwei_common import get_api_key
except ImportError:
    from zhiwei_common import get_api_key

# 引入 distiller 以便复用 ASR 逻辑 (v6.0; v3.3 增 mimo-asr + MLX 本地)
try:
    from scripts.douyin_distiller import (
        DashScopeASRTranscriber, MimoASRTranscriber,
        LocalMLXWhisperTranscriber, AppConfig,
    )
except ImportError:
    DashScopeASRTranscriber = None
    MimoASRTranscriber = None
    LocalMLXWhisperTranscriber = None
    AppConfig = None

# 引入 Mimo TTS 客户端
try:
    from mimo_tts import MimoTTSClient
except ImportError:
    MimoTTSClient = None

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
    """调用统一多模态出口分析图片

    2026-07-31 修复：旧实现构建了 messages（含 image_url）却从未使用，
    实际只把文本 prompt 传给 client.call("vision", ...)——图片从没进过模型，
    所谓"图片分析"实为凭文本瞎编。现改调 zhiwei_common.llm.call_vision，
    真正将图片送入模型（已实测验证）。
    """
    try:
        from zhiwei_common.llm import call_vision

        prompt = question if question else "请分析这张图片，描述内容并提取关键信息。"
        print(f"🖼️ 调用多模态模型分析图片... 问题: {prompt[:30]}...")

        success, content = call_vision(
            prompt, image_b64=image_base64, image_mime="image/jpeg", max_tokens=2000
        )

        if success:
            print(f"✅ 图片分析完成: {len(content)} 字符")
            return f"🖼️ **图片分析结果**\n\n{content}"
        print(f"❌ 图片分析失败: {content}")
        return f"❌ 图片分析失败: {content}"
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
    """提取视频 URL

    支持格式：
    - https://v.douyin.com/xxx/ (短链)
    - https://www.douyin.com/video/xxx (长链)
    - douyin.com/video/xxx (无协议前缀，抖音新分享格式)
    - https://douyin.com/video/xxx (无 www)
    - B站、YouTube 等
    """
    patterns = [
        # 抖音短链
        r'(https?://v\.douyin\.com/[A-Za-z0-9_-]+/?)',
        # 抖音长链（带 www）
        r'(https?://www\.douyin\.com/video/\d+)',
        # 抖音长链（无 www，新版分享格式）
        r'(https?://douyin\.com/video/\d+)',
        # 抖音长链（无协议前缀，需要补全）
        r'(?<![\w./])(douyin\.com/video/\d+)',
        # YouTube
        r'(https?://(?:www\.)?youtube\.com/watch\?v=[A-Za-z0-9_-]+)',
        r'(https?://youtu\.be/[A-Za-z0-9_-]+)',
        # B站
        r'(https?://(?:www\.)?bilibili\.com/video/[A-Za-z0-9_-]+)',
        r'(https?://b23\.tv/[A-Za-z0-9_-]+)'
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            url = match.group(1)
            # 补全协议前缀
            if url.startswith('douyin.com'):
                url = 'https://' + url
            return url
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
    """总结网页 URL，统一调用 zhiwei-rag 的 url_ingest 引擎 (v6.0)
    
    优势：支持 v5.4 的博主式提炼、自动 RAG 关联和更鲁棒的抓取逻辑。
    """
    try:
        logger.info(f"🌐 正在调用 url_ingest 蒸馏网页: {url}")
        
        rag_venv = "/Users/liufang/zhiwei-rag/venv/bin/python3"
        url_ingest_script = "/Users/liufang/zhiwei-rag/ingest/url_ingest.py"
        
        # 调用 url_ingest 并启用蒸馏模式
        # 注意：这里我们只取 stdout 返回的摘要内容
        cmd = [
            rag_venv, url_ingest_script, url, 
            "--distill", 
            "--output", "stdout" # 假设 url_ingest 支持此参数或我们通过解析日志获取
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        
        if result.returncode == 0:
            summary = result.stdout.strip()
            if "📄 **网页摘要**" in summary or len(summary) > 50:
                return summary
            return f"📄 **网页摘要**\n\n{summary}"
        else:
            logger.error(f"url_ingest 失败: {result.stderr}")
            return f"❌ 网页总结失败 (url_ingest 异常)"

    except Exception as e:
        logger.error(f"summarize_url 异常: {e}")
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


def _wants_vision(text: str) -> bool:
    """判断用户是否请求视觉分析(v3.0)

    触发方式: 消息以 /video vision 开头,或消息同时含视频链接与「视觉分析」/「抽帧」关键词。
    典型场景: 看完视频后把链接带关键词再发一次,触发抽帧+VLM 图表提取并重蒸馏。
    """
    stripped = text.strip().lower()
    if stripped.startswith("/video vision"):
        return True
    return any(kw in text for kw in ("视觉分析", "抽帧"))


def _extract_md_section(text: str, heading: str, max_chars: int = 500) -> str:
    """从 markdown 里取指定二级标题下的正文（不含子标题以后的内容）"""
    pattern = re.compile(
        r"^##\s*" + re.escape(heading) + r"\s*$(.*?)(?=^##\s|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(text)
    if not m:
        return ""
    body = m.group(1).strip()
    # 去掉空行与 Obsidian 内链语法，保留可读要点
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    body = "\n".join(lines)
    return body[:max_chars].rstrip()


def _build_video_digest(output_path: str, title: str) -> str:
    """⭐ N2 (2026-07-31): 视频处理完成后直接回推要点摘要

    背景：旧行为只回“文件已生成 + 请到 Obsidian 查看”，而粘链接是用户
    88% 的真实用途（入站消息统计），每次都要自己去翻笔记，闭环断在最后一步。
    现从生成的笔记中抽“核心洞察/摘要/行动建议”回推；读文件失败则降级为原行为。
    """
    header = f"✅ 视频知识笔记已生成\n\n📝 **{title}**"
    try:
        content = Path(output_path).read_text(errors="ignore")
    except Exception as e:
        logger.warning(f"摘要抽取失败，降级回文件路径: {e}")
        return f"{header}\n\n📁 {output_path}"

    parts = [header]
    for label, heading, limit in (
        ("💡 核心洞察", "核心洞察", 420),
        ("📊 量化指标", "量化指标", 200),
        ("✅ 行动建议", "行动建议", 260),
    ):
        body = _extract_md_section(content, heading, limit)
        if body:
            parts.append(f"{label}\n{body}")

    # 核心洞察缺失时用“摘要”兜底，避免只回一个标题
    if len(parts) == 1:
        fallback = _extract_md_section(content, "摘要", 420)
        if fallback:
            parts.append(f"📄 摘要\n{fallback}")

    parts.append(f"📁 完整笔记: {output_path}")
    return "\n\n".join(parts)


def process_video(text: str, message_id: str = None) -> str:
    """处理视频分析 - 调用宿主机 Distiller

    v2.0 新增：
    - 详细错误分类和记录
    - 自动重试临时性错误
    - 严重错误飞书告警
    v3.0 新增：按需视觉分析(--vision,抽帧+VLM 图表提取)
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
        # 使用共享 venv (v2.0 合并后)
        venv_python = os.path.expanduser("~/zhiwei-shared-venv/bin/python")

        cmd = [
            venv_python, distiller_path,
            "--from-text", text,
            "--output-dir", os.path.expanduser("~/Documents/ZhiweiVault/70-79_个人笔记/75_视频笔记_Video-Distill"),
        ]

        # ⭐ v3.0: 按需视觉分析(--vision 隐含 --force,重跑时复用转写缓存)
        vision_mode = _wants_vision(text)
        if vision_mode:
            cmd.append("--vision")
            logger.info("🔍 视觉分析模式: 抽帧 + VLM 图表提取")

        # 根据平台选择 cookies 策略
        if "bilibili.com" in url or "b23.tv" in url:
            # B站需要从浏览器读取 cookies（AI 字幕需登录态；网页解析已改走官方 API）
            cmd.extend(["--cookies-from-browser", "chrome"])
        elif "douyin.com" in url or "iesdouyin.com" in url:
            # 抖音使用 cookies 文件
            cmd.extend(["--cookies", os.path.expanduser("~/zhiwei-bot/secrets/douyin_cookies.txt")])
        elif "youtube.com" in url or "youtu.be" in url:
            # ⭐ v3.1: YouTube 需登录 cookies 才能过 bot 检测拿字幕列表；
            # 网络出口由 distiller 内部自动走日本 VM 的 SOCKS5 隔离(平台感知)。
            yt_ck = os.path.expanduser("~/zhiwei-bot/secrets/youtube_cookies.txt")
            if os.path.exists(yt_ck):
                cmd.extend(["--cookies", yt_ck])
            else:
                logger.warning("YouTube cookies 缺失，字幕提取可能被 bot 检测拦截")
        # 其余平台：不带 cookies（避免无谓加载抖音 cookies 而在日志里产生 "cookie" 字样干扰错误归类）

        logger.info(f"🎬 调用 Distiller: {' '.join(cmd[:3])}...")

        # ⭐ 2026-06-02: 子进程依赖预检查（超时/异常降级，不阻塞主流程）
        # 注：原 timeout=5 在系统高负载时易把"冷启动进程 + import"拖超时，
        # 误报"环境检查失败"并拦在主命令之前。改为宽松超时 + 失败降级继续执行，
        # 真正的依赖问题由下方主命令（timeout=600）自行暴露。
        try:
            check = subprocess.run(
                [venv_python, "-c", "import dotenv; import requests; import dashscope; import yt_dlp"],
                capture_output=True, text=True, timeout=20
            )
            if check.returncode != 0:
                logger.error(f"Distiller 依赖检查失败: {check.stderr.strip()[:200]}")
                return "❌ 视频分析依赖不完整，请联系管理员修复"
        except subprocess.TimeoutExpired:
            logger.warning("Distiller 依赖预检查超时（疑似系统繁忙），降级继续执行主流程")
        except Exception as _e:
            logger.warning(f"Distiller 依赖预检查异常，降级继续执行: {_e}")

        try:
            # ⭐ v3.0: vision 模式含视频下载+抽帧+逐帧 VLM,耗时更长
            _timeout = 1800 if vision_mode else 900
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=_timeout)
        except subprocess.TimeoutExpired as e:
            # ⭐ 2026-07-27: 超时时记录 partial output，便于定位卡在哪一步
            if e.stdout:
                logger.error(f"Distiller timeout - stdout (last 2000 chars):\n{e.stdout[-2000:]}")
            if e.stderr:
                logger.error(f"Distiller timeout - stderr (last 2000 chars):\n{e.stderr[-2000:]}")
            raise

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

            # ⭐ 2026-06-02: 错误脱敏，不向用户暴露堆栈
            friendly_msg = {
                "timeout": "❌ 视频分析超时（超过 15 分钟），请检查链接是否有效",
                "network_error": "❌ 视频下载失败（网络错误），请检查链接是否有效后重试",
                "module_error": "❌ 视频分析模块异常，请联系管理员",
                "unknown": "❌ 视频处理失败（内部错误），已记录日志",
            }.get(error_type, "❌ 视频处理失败，请稍后重试")
            logger.error(f"Distiller 失败 (type={error_type}): {str(error_message)[:200]}")
            return friendly_msg

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
                # ⭐ 2026-07-31 N2: 不再只回文件路径（旧行为要求用户自己去
                # Obsidian 翻），直接把笔记里的要点摘要回推，形成闭环。
                return _build_video_digest(output_path, title)
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
    """转录语音文件为文字

    v3.3 (2026-07-31): 原单用 DashScope, 但 DASHSCOPE_API_KEY 已 401 失效,
    飞书语音消息识别一直静默失败。改为 mimo-asr 云端首选(短语音实测 4.7s/60s),
    本地 MLX Whisper 兜底——两者都不依赖已死的 DashScope key。
    """
    from pathlib import Path as _P
    audio_obj = _P(audio_path)
    try:
        cfg = AppConfig() if AppConfig else None

        # 1. mimo-asr 云端首选(飞书语音多为短语音, mimo 快且准)
        if MimoASRTranscriber and cfg and getattr(cfg, "mimo_api_key", ""):
            try:
                tr = MimoASRTranscriber(cfg.mimo_api_key, cfg.mimo_api_base, cfg.mimo_asr_model)
                res = tr.transcribe(audio_obj)
                if res and res.full_text:
                    logger.info(f"飞书语音 mimo-asr 成功: {len(res.full_text)} 字")
                    return res.full_text
                logger.warning("mimo-asr 空结果, 降级本地 MLX")
            except Exception as e:
                logger.warning(f"mimo-asr 失败: {e}, 降级本地 MLX")

        # 2. 本地 MLX Whisper 兜底(免费, 不依赖云端 key)
        if LocalMLXWhisperTranscriber:
            try:
                local = LocalMLXWhisperTranscriber(getattr(cfg, "local_asr_model", "small") if cfg else "small")
                if local.is_available():
                    res = local.transcribe(audio_obj)
                    if res and res.full_text:
                        logger.info(f"飞书语音 本地 MLX 成功: {len(res.full_text)} 字")
                        return res.full_text
            except Exception as e:
                logger.error(f"本地 MLX 也失败: {e}")

        logger.error("语音转写全部失败(mimo-asr + 本地 MLX)")
        return None

    except Exception as e:
        logger.error(f"ASR 转录异常: {e}")
        return None
    finally:
        # 清理原文件
        if audio_path and os.path.exists(audio_path):
            try:
                os.remove(audio_path)
            except: pass


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


# TTS 语音回复状态管理
tts_enabled_users = set()  # 已开启 TTS 回复的用户集合

# 全局依赖（由 init_media_handler 注入）
send_audio_reply = None  # 飞书语音发送函数


def init_media_handler_with_audio(global_send_audio_reply):
    """初始化媒体处理模块的语音发送依赖"""
    global send_audio_reply
    send_audio_reply = global_send_audio_reply


def text_to_speech_reply(text: str, message_id: str) -> bool:
    """将文字通过 TTS 转为语音并发送

    Args:
        text: 要转换的文本
        message_id: 原始消息 ID

    Returns:
        是否成功发送
    """
    if not MimoTTSClient:
        logger.warning("MimoTTSClient 不可用，跳过 TTS 回复")
        return False

    try:
        # 获取 API key
        api_key = get_api_key(["MIMO_API_KEY", "BAILIAN_API_KEY", "CODING_PLAN_API_KEY", "DASHSCOPE_API_KEY"])
        if not api_key:
            logger.warning("MIMO_API_KEY 未配置，跳过 TTS 回复")
            return False

        # 清理文本中的 markdown 格式
        clean_text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)  # 去掉 ** 加粗
        clean_text = re.sub(r'[*_`~]', '', clean_text)          # 去掉其他格式符
        clean_text = clean_text.strip()

        if not clean_text or len(clean_text) < 2:
            return False

        # 调用 TTS
        tts_client = MimoTTSClient(api_key=api_key)
        audio_path = tts_client.synthesize(clean_text)

        if not audio_path or not os.path.exists(audio_path):
            logger.warning("TTS 合成失败，跳过语音回复")
            return False

        # 发送语音消息
        if send_audio_reply:
            result = send_audio_reply(message_id, audio_path)
        else:
            logger.warning("send_audio_reply 未初始化，跳过发送")
            result = False

        # 清理临时文件
        try:
            os.remove(audio_path)
        except OSError:
            pass

        return result

    except Exception as e:
        logger.error(f"TTS 回复异常: {e}")
        return False


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
