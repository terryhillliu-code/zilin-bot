#!/usr/bin/env python3
"""
抖音知识蒸馏引擎 MVP
从抖音/短视频链接自动生成 Obsidian Markdown 笔记

核心流程：
URL → 解析 → 字幕/ASR → LLM 蒸馏 → Markdown 输出

作者: 知微系统
版本: v1.0.0
"""

import os
import re
import sys
import json
import tempfile
import subprocess
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)


# ============================================================================
# 数据模型
# ============================================================================

@dataclass
class VideoInfo:
    """视频元信息"""
    original_url: str
    resolved_url: str
    platform: str = "unknown"
    title: str = ""
    author: str = ""
    duration: int = 0  # 秒
    description: str = ""


@dataclass
class TranscriptSegment:
    """转录片段"""
    start: float  # 秒
    end: float
    text: str


@dataclass
class TranscriptResult:
    """转录结果"""
    segments: list[TranscriptSegment] = field(default_factory=list)
    full_text: str = ""
    source: str = "unknown"  # platform_subtitle | dashscope_asr | local_asr
    language: str = "zh"
    confidence: float = 0.0

    def to_text(self) -> str:
        """转换为带时间戳的文本"""
        if not self.segments:
            return self.full_text
        lines = []
        for seg in self.segments:
            timestamp = self._format_timestamp(seg.start)
            lines.append(f"[{timestamp}] {seg.text}")
        return "\n".join(lines)

    @staticmethod
    def _format_timestamp(seconds: float) -> str:
        """格式化时间戳为 MM:SS"""
        mins = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{mins:02d}:{secs:02d}"


@dataclass
class DistilledKnowledge:
    """蒸馏后的知识结构"""
    title: str
    one_liner: str  # 一句话核心
    key_points: list[dict]  # [{timestamp, point}]
    summary: str
    tags: list[str]
    action_items: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)


# ============================================================================
# 配置管理
# ============================================================================

class AppConfig:
    """应用配置管理"""

    def __init__(self):
        # 按优先级加载 .env 文件：复用现有配置
        env_paths = [
            Path.home() / "zhiwei-bot" / ".env",       # 主配置（已有 DASHSCOPE_API_KEY）
            Path.home() / ".secrets" / "zhiwei.env",   # 备用配置（OPENAI_API_KEY）
            Path(__file__).parent / ".env",            # 脚本目录配置
        ]

        loaded = False
        for env_path in env_paths:
            if env_path.exists():
                load_dotenv(env_path, override=False)  # 不覆盖已加载的
                logger.info(f"Loaded config from {env_path}")
                loaded = True

        if not loaded:
            logger.warning("No .env file found, using environment variables")

        # API 配置
        self.dashscope_api_key = os.getenv("DASHSCOPE_API_KEY", "")
        self.qwen_model = os.getenv("QWEN_MODEL", "qwen-plus")
        self.asr_model = os.getenv("ASR_MODEL", "paraformer-realtime-v2")
        self.asr_policy = os.getenv("ASR_POLICY", "auto")
        self.local_asr_model = os.getenv("LOCAL_ASR_MODEL", "small")

        # 输出配置
        output_dir = os.getenv("OUTPUT_DIR", "~/Documents/ZhiweiVault/Inbox")
        self.output_dir = Path(output_dir).expanduser()

        # 日志级别
        log_level = os.getenv("LOG_LEVEL", "INFO")
        logging.getLogger().setLevel(getattr(logging, log_level.upper(), logging.INFO))

        # 验证必要配置
        if not self.dashscope_api_key:
            logger.warning("DASHSCOPE_API_KEY not set, ASR and LLM features will be limited")

    def validate(self) -> bool:
        """验证配置完整性"""
        if not self.dashscope_api_key:
            logger.error("DASHSCOPE_API_KEY is required")
            return False
        return True

    def ensure_output_dir(self):
        """确保输出目录存在"""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Output directory: {self.output_dir}")


# ============================================================================
# 分享文本提取器
# ============================================================================

class ShareTextExtractor:
    """从各种格式的分享文本中提取视频 URL"""

    # 支持的 URL 模式（按优先级排序）
    URL_PATTERNS = [
        r'https?://v\.douyin\.com/[A-Za-z0-9_/]+',           # 抖音短链
        r'https?://www\.douyin\.com/video/\d+',              # 抖音长链
        r'https?://www\.tiktok\.com/@[^/]+/video/\d+',       # TikTok
        r'https?://vm\.tiktok\.com/[A-Za-z0-9]+',            # TikTok 短链
        r'https?://www\.bilibili\.com/video/[A-Za-z0-9]+',   # B站
        r'https?://b23\.tv/[A-Za-z0-9]+',                    # B站短链
        r'https?://xhslink\.com/[A-Za-z0-9/]+',              # 小红书短链
        r'https?://www\.xiaohongshu\.com/explore/[a-f0-9]+', # 小红书长链
        r'https?://v\.kuaishou\.com/[A-Za-z0-9]+',           # 快手
        r'https?://www\.kuaishou\.com/short-video/[A-Za-z0-9]+',
        r'https?://t\.cn/[A-Za-z0-9]+',                      # 微博短链
        r'https?://weibo\.com/tv/show/[A-Za-z0-9]+',         # 微博视频
    ]

    @classmethod
    def extract(cls, text: str) -> list[str]:
        """
        从文本中提取所有视频 URL，去重并保持顺序

        处理逻辑：
        1. 用正则依次匹配所有模式
        2. 清理 URL 尾部可能粘连的中文标点
        3. 清理 URL 尾部的口令垃圾（如 "qeO:/"）
        4. 去重
        """
        urls = []
        seen = set()
        for pattern in cls.URL_PATTERNS:
            for match in re.finditer(pattern, text):
                url = match.group(0)
                # 清理尾部中文标点和口令垃圾
                url = url.rstrip('，。！？、；：""''）】》')
                # 清理尾部可能的口令格式 (如 "qeO:/", "abc@123")
                url = re.sub(r'[A-Za-z0-9@._:/]+$', '', url) is False and url or url
                # 再次清理尾部标点
                url = url.rstrip('，。！？、；：""''）】》./')
                if url and url not in seen:
                    seen.add(url)
                    urls.append(url)
        return urls

    @classmethod
    def extract_first(cls, text: str) -> Optional[str]:
        """提取第一个 URL，没有则返回 None"""
        urls = cls.extract(text)
        return urls[0] if urls else None


# ============================================================================
# 已处理记录存储
# ============================================================================

class ProcessedStore:
    """记录已处理的视频，避免重复蒸馏"""

    DEFAULT_PATH = os.path.expanduser("~/zhiwei-bot/data/processed_videos.json")

    def __init__(self, path: str = None):
        self.path = path or self.DEFAULT_PATH
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._load()

    def _load(self):
        """加载已处理记录"""
        if os.path.exists(self.path):
            try:
                with open(self.path, 'r', encoding='utf-8') as f:
                    self.data = json.load(f)
            except (json.JSONDecodeError, IOError):
                logger.warning(f"Failed to load processed store, starting fresh")
                self.data = {}
        else:
            self.data = {}

    def _save(self):
        """保存记录到文件"""
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def is_processed(self, resolved_url: str) -> bool:
        """检查这个 URL 是否已经处理过"""
        return resolved_url in self.data

    def get_record(self, resolved_url: str) -> Optional[dict]:
        """获取已处理记录"""
        return self.data.get(resolved_url)

    def mark_processed(self, resolved_url: str, output_path: str, title: str = ""):
        """标记这个 URL 已处理"""
        self.data[resolved_url] = {
            "processed_at": datetime.now().isoformat(),
            "output_path": str(output_path),
            "title": title
        }
        self._save()
        logger.info(f"Marked as processed: {resolved_url}")

    def get_stats(self) -> dict:
        """获取统计信息"""
        return {"total_processed": len(self.data)}


# ============================================================================
# URL 解析器
# ============================================================================

class URLResolver:
    """URL 解析与重定向追踪"""

    # 已知短视频平台
    PLATFORMS = {
        "douyin": ["douyin.com", "v.douyin.com"],
        "tiktok": ["tiktok.com", "vm.tiktok.com"],
        "bilibili": ["bilibili.com", "b23.tv"],
        "xiaohongshu": ["xiaohongshu.com", "xhslink.com"],
        "kuaishou": ["kuaishou.com", "kuaishou.cn"],
        "weibo": ["weibo.com", "weibo.cn", "t.cn"],
    }

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15"
        })

    def resolve(self, url: str) -> VideoInfo:
        """解析 URL，追踪重定向，提取视频信息"""
        logger.info(f"Resolving URL: {url}")

        # 追踪重定向
        resolved_url = self._follow_redirects(url)
        logger.info(f"Resolved to: {resolved_url}")

        # 识别平台
        platform = self._identify_platform(resolved_url)

        return VideoInfo(
            original_url=url,
            resolved_url=resolved_url,
            platform=platform
        )

    def _follow_redirects(self, url: str, max_redirects: int = 10) -> str:
        """追踪重定向获取最终 URL"""
        current_url = url
        for _ in range(max_redirects):
            try:
                response = self.session.head(
                    current_url,
                    allow_redirects=False,
                    timeout=10
                )
                if 300 <= response.status_code < 400:
                    next_url = response.headers.get("Location", "")
                    if next_url:
                        logger.debug(f"Redirect: {current_url} -> {next_url}")
                        current_url = next_url
                        continue
                break
            except requests.RequestException as e:
                logger.warning(f"Error following redirect: {e}")
                break
        return current_url

    def _identify_platform(self, url: str) -> str:
        """识别视频平台"""
        hostname = urlparse(url).netloc.lower()
        for platform, domains in self.PLATFORMS.items():
            if any(domain in hostname for domain in domains):
                return platform
        return "unknown"


# ============================================================================
# 媒体提取器
# ============================================================================

class MediaExtractor:
    """使用 yt-dlp 提取字幕和音频"""

    def __init__(self, cookies_browser: Optional[str] = None):
        self.yt_dlp_path = self._find_yt_dlp()
        self.cookies_browser = cookies_browser

    def _find_yt_dlp(self) -> str:
        """查找 yt-dlp 可执行文件"""
        result = subprocess.run(
            ["which", "yt-dlp"],
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            return result.stdout.strip()
        raise RuntimeError("yt-dlp not found. Install with: pip install yt-dlp")

    def extract_subtitles(self, video_info: VideoInfo) -> Optional[TranscriptResult]:
        """提取平台字幕（如果有）"""
        import yt_dlp

        logger.info(f"Extracting subtitles from {video_info.resolved_url}")

        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["zh-Hans", "zh", "zh-CN", "en"],
        }

        # 添加 cookies 支持
        if self.cookies_browser:
            ydl_opts["cookiesfrombrowser"] = (self.cookies_browser,)

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(video_info.resolved_url, download=False)

                # 更新视频信息
                video_info.title = info.get("title", "")
                video_info.author = info.get("uploader", "")
                video_info.duration = info.get("duration", 0)
                video_info.description = info.get("description", "")

                # 获取字幕
                subtitles = info.get("subtitles", {}) or info.get("automatic_captions", {})

                if not subtitles:
                    logger.info("No subtitles found")
                    return None

                # 优先选择中文字幕
                for lang in ["zh-Hans", "zh", "zh-CN"]:
                    if lang in subtitles:
                        subtitle_url = subtitles[lang][0]["url"]
                        return self._download_and_parse_subtitles(subtitle_url)

                # 其次选择英文字幕
                if "en" in subtitles:
                    subtitle_url = subtitles["en"][0]["url"]
                    return self._download_and_parse_subtitles(subtitle_url)

                logger.info("No usable subtitles found")
                return None

        except Exception as e:
            logger.error(f"Error extracting subtitles: {e}")
            return None

    def _download_and_parse_subtitles(self, url: str) -> TranscriptResult:
        """下载并解析字幕文件"""
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            content = response.text

            # 解析字幕格式（支持 SRT、VTT）
            segments = self._parse_subtitle_content(content)

            return TranscriptResult(
                segments=segments,
                full_text=" ".join(s.text for s in segments),
                source="platform_subtitle",
                language="zh",
                confidence=1.0
            )
        except Exception as e:
            logger.error(f"Error downloading subtitles: {e}")
            return TranscriptResult()

    def _parse_subtitle_content(self, content: str) -> list[TranscriptSegment]:
        """解析字幕内容（SRT/VTT 格式）"""
        segments = []

        # SRT 时间戳正则
        time_pattern = r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2}),(\d{3})"

        lines = content.strip().split("\n")
        i = 0

        while i < len(lines):
            line = lines[i].strip()

            # 查找时间戳行
            match = re.search(time_pattern, line)
            if match:
                # 解析时间
                start = self._srt_time_to_seconds(match.group(1), match.group(2),
                                                   match.group(3), match.group(4))
                end = self._srt_time_to_seconds(match.group(5), match.group(6),
                                                 match.group(7), match.group(8))

                # 收集字幕文本
                i += 1
                text_lines = []
                while i < len(lines) and lines[i].strip() and not re.search(time_pattern, lines[i]):
                    text_lines.append(lines[i].strip())
                    i += 1

                text = " ".join(text_lines)
                if text:
                    segments.append(TranscriptSegment(start=start, end=end, text=text))
            else:
                i += 1

        return segments

    @staticmethod
    def _srt_time_to_seconds(h: str, m: str, s: str, ms: str) -> float:
        """将 SRT 时间转换为秒"""
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000

    def extract_audio(self, video_info: VideoInfo, output_path: Path) -> bool:
        """提取音频文件"""
        import yt_dlp

        logger.info(f"Extracting audio to {output_path}")

        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "format": "bestaudio/best",
            "outtmpl": str(output_path.with_suffix("")),
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "128",
            }],
        }

        # 添加 cookies 支持
        if self.cookies_browser:
            ydl_opts["cookiesfrombrowser"] = (self.cookies_browser,)

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([video_info.resolved_url])

            # 检查输出文件
            mp3_path = output_path.with_suffix(".mp3")
            if mp3_path.exists():
                logger.info(f"Audio extracted: {mp3_path}")
                return True
            return False

        except Exception as e:
            logger.error(f"Error extracting audio: {e}")
            return False


# ============================================================================
# ASR 抽象基类
# ============================================================================

class BaseTranscriber(ABC):
    """ASR 转录器抽象基类"""

    @abstractmethod
    def transcribe(self, audio_path: Path) -> TranscriptResult:
        """转录音频文件"""
        pass

    @abstractmethod
    def is_available(self) -> bool:
        """检查转录器是否可用"""
        pass


# ============================================================================
# DashScope ASR 转录器
# ============================================================================

class DashScopeASRTranscriber(BaseTranscriber):
    """阿里云百炼 ASR 转录器"""

    def __init__(self, api_key: str, model: str = "sensevoice-v1"):
        self.api_key = api_key
        self.model = model
        self._available = bool(api_key)

    def is_available(self) -> bool:
        return self._available

    def transcribe(self, audio_path: Path) -> TranscriptResult:
        """使用 DashScope ASR 转录

        注意：Transcription.async_call() 需要 OSS URL，不支持 file:// 本地协议。
        改用 Recognition.call() 处理本地音频文件。
        Recognition API 要求：采样率 16000Hz，单声道。
        """
        if not self._available:
            raise RuntimeError("DashScope API key not configured")

        logger.info(f"Transcribing with DashScope {self.model}: {audio_path}")

        try:
            import dashscope
            from dashscope.audio.asr import Recognition, RecognitionCallback

            dashscope.api_key = self.api_key

            # 检测并转换音频格式（Recognition API 需要 16kHz 单声道）
            audio_path = self._ensure_audio_format(audio_path)

            # 检测音频格式
            suffix = audio_path.suffix.lower().lstrip('.')
            audio_format = suffix if suffix in ['mp3', 'wav', 'pcm', 'opus', 'm4a', 'aac'] else 'mp3'

            # 定义回调类收集结果
            class TranscribeCallback(RecognitionCallback):
                def __init__(self):
                    self.result = None
                    self.error = None

                def on_result(self, result):
                    self.result = result

                def on_error(self, error):
                    self.error = error

            # 创建 Recognition 实例
            callback = TranscribeCallback()
            recognition = Recognition(
                model=self.model,
                format=audio_format,
                sample_rate=16000,
                callback=callback
            )

            # 调用同步识别
            result = recognition.call(file=str(audio_path.absolute()))

            if result.status_code == 200:
                return self._parse_recognition_result(result)
            else:
                logger.error(f"DashScope Recognition error: {result.message}")
                return TranscriptResult()

        except ImportError:
            logger.error("dashscope not installed. Run: pip install dashscope")
            return TranscriptResult()
        except Exception as e:
            logger.error(f"DashScope transcription error: {e}")
            return TranscriptResult()

    def _parse_recognition_result(self, result) -> TranscriptResult:
        """解析 Recognition API 返回结果"""
        segments = []
        full_text = ""

        try:
            # Recognition 返回结果结构
            if hasattr(result, 'output') and result.output:
                output = result.output

                # 提取句子列表
                if 'sentence' in output:
                    for sentence in output['sentence']:
                        text = sentence.get('text', '')
                        full_text += text

                        segments.append(TranscriptSegment(
                            start=sentence.get('begin_time', 0) / 1000,
                            end=sentence.get('end_time', 0) / 1000,
                            text=text
                        ))
                elif 'text' in output:
                    # 简单文本结果
                    full_text = output['text']

            return TranscriptResult(
                segments=segments,
                full_text=full_text,
                source="dashscope_recognition",
                language="zh",
                confidence=0.95
            )

        except Exception as e:
            logger.error(f"Error parsing Recognition result: {e}")
            return TranscriptResult(full_text=full_text, source="dashscope_recognition")

    def _ensure_audio_format(self, audio_path: Path) -> Path:
        """确保音频格式符合 Recognition API 要求（16kHz 单声道）

        yt-dlp 下载的音频通常是 48kHz 立体声，需要转换。
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
                logger.warning(f"ffprobe failed, using original audio: {result.stderr}")
                return audio_path

            # 解析输出：格式为 "sample_rate,channels"
            output = result.stdout.strip()
            parts = output.split(',')

            if len(parts) >= 2:
                sample_rate = int(parts[0])
                channels = int(parts[1])

                logger.debug(f"Audio format: {sample_rate}Hz, {channels} channels")

                # 检查是否需要转换（非16kHz或非单声道）
                if sample_rate != 16000 or channels != 1:
                    converted_path = audio_path.with_suffix(".converted.mp3")
                    logger.info(f"Converting audio: {sample_rate}Hz/{channels}ch -> 16000Hz/1ch")

                    convert_cmd = [
                        "ffmpeg", "-y", "-i", str(audio_path),
                        "-ar", "16000", "-ac", "1", "-f", "mp3",
                        str(converted_path)
                    ]
                    conv_result = subprocess.run(convert_cmd, capture_output=True, timeout=120)

                    if conv_result.returncode == 0 and converted_path.exists():
                        logger.info(f"Audio converted: {converted_path}")
                        return converted_path
                    else:
                        logger.warning(f"ffmpeg conversion failed: {conv_result.stderr.decode()}")
                        return audio_path
                else:
                    logger.debug("Audio format already correct (16kHz mono)")
                    return audio_path
            else:
                logger.warning(f"Unexpected ffprobe output: {output}")
                return audio_path

        except subprocess.TimeoutExpired:
            logger.warning("ffprobe/ffmpeg timeout, using original audio")
            return audio_path
        except Exception as e:
            logger.warning(f"Audio format check failed: {e}, using original audio")
            return audio_path

    # ============================================================================
# 本地 MLX Whisper 转录器
# ============================================================================

class LocalMLXWhisperTranscriber(BaseTranscriber):
    """本地 MLX Whisper 转录器（Apple Silicon 优化）"""

    def __init__(self, model: str = "small"):
        self.model = model
        self._available = self._check_availability()

    def _check_availability(self) -> bool:
        """检查 mlx-whisper 是否可用"""
        try:
            import mlx_whisper
            return True
        except ImportError:
            logger.warning("mlx-whisper not installed. Run: pip install mlx-whisper")
            return False

    def is_available(self) -> bool:
        return self._available

    def transcribe(self, audio_path: Path) -> TranscriptResult:
        """使用 MLX Whisper 本地转录"""
        if not self._available:
            raise RuntimeError("mlx-whisper not available")

        logger.info(f"Transcribing with MLX Whisper {self.model}: {audio_path}")

        try:
            import mlx_whisper

            # 执行转录
            result = mlx_whisper.transcribe(
                str(audio_path),
                path_or_hf_repo=f"mlx-community/whisper-{self.model}"

            )

            # 解析结果
            segments = []
            full_text = result.get("text", "")

            if "segments" in result:
                for seg in result["segments"]:
                    segments.append(TranscriptSegment(
                        start=seg.get("start", 0),
                        end=seg.get("end", 0),
                        text=seg.get("text", "")
                    ))

            return TranscriptResult(
                segments=segments,
                full_text=full_text,
                source="local_asr",
                language="zh",
                confidence=0.9
            )

        except Exception as e:
            logger.error(f"MLX Whisper transcription error: {e}")
            return TranscriptResult()


# ============================================================================
# 转录提供者（路由编排）
# ============================================================================

class TranscriptProvider:
    """转录服务路由"""

    def __init__(self, config: AppConfig, cookies_browser: Optional[str] = None):
        self.config = config
        self.cookies_browser = cookies_browser
        self.media_extractor = MediaExtractor(cookies_browser)
        self.dashscope_transcriber = DashScopeASRTranscriber(
            config.dashscope_api_key,
            config.asr_model
        )
        self.local_transcriber = LocalMLXWhisperTranscriber(config.local_asr_model)

    def get_transcript(self, video_info: VideoInfo) -> TranscriptResult:
        """获取转录文本，按策略选择方法"""
        policy = self.config.asr_policy

        # 策略：auto - 优先尝试字幕
        if policy == "auto":
            # 尝试提取平台字幕
            subtitle_result = self.media_extractor.extract_subtitles(video_info)
            if subtitle_result and subtitle_result.full_text:
                logger.info("Successfully extracted platform subtitles")
                return subtitle_result

            logger.info("No subtitles found, falling back to ASR")

        # ASR 转录
        return self._transcribe_with_asr(video_info)

    def _transcribe_with_asr(self, video_info: VideoInfo) -> TranscriptResult:
        """使用 ASR 转录"""
        with tempfile.TemporaryDirectory(prefix="distill_") as tmpdir:
            tmpdir_path = Path(tmpdir)
            audio_path = tmpdir_path / "audio"

            # 提取音频
            if not self.media_extractor.extract_audio(video_info, audio_path):
                logger.error("Failed to extract audio")
                return TranscriptResult()

            # 优先使用 DashScope
            if self.dashscope_transcriber.is_available():
                logger.info("Using DashScope ASR")
                return self.dashscope_transcriber.transcribe(audio_path.with_suffix(".mp3"))

            # 兜底使用本地 ASR
            if self.local_transcriber.is_available():
                logger.info("Using local MLX Whisper ASR")
                return self.local_transcriber.transcribe(audio_path.with_suffix(".mp3"))

            logger.error("No ASR service available")
            return TranscriptResult()


# ============================================================================
# 转录后处理
# ============================================================================

class TranscriptPostProcessor:
    """转录文本后处理"""

    @staticmethod
    def clean(transcript: TranscriptResult) -> TranscriptResult:
        """清洗转录文本"""
        if not transcript.full_text:
            return transcript

        # 清理文本
        cleaned_text = transcript.full_text

        # 移除重复词
        cleaned_text = re.sub(r'(.)\1{3,}', r'\1\1\1', cleaned_text)

        # 移除多余空格
        cleaned_text = re.sub(r'\s+', ' ', cleaned_text)

        # 移除无意义标点
        cleaned_text = re.sub(r'[,，]{2,}', '，', cleaned_text)

        # 更新全文
        transcript.full_text = cleaned_text.strip()

        # 清理片段文本
        for seg in transcript.segments:
            seg.text = re.sub(r'\s+', ' ', seg.text).strip()

        return transcript

    @staticmethod
    def add_noise_tags(transcript: TranscriptResult) -> list[str]:
        """检测噪音标签"""
        noise_patterns = [
            (r'\[音乐\]|\[BGM\]|🎵|♪', '背景音乐'),
            (r'\[掌声\]|👏', '掌声'),
            (r'\[笑声\]|😂|哈哈', '笑声'),
            (r'\[噪音\]|🔊', '环境噪音'),
        ]

        tags = []
        text = transcript.full_text
        for pattern, tag in noise_patterns:
            if re.search(pattern, text):
                tags.append(tag)
        return tags


# ============================================================================
# 知识蒸馏器
# ============================================================================

class KnowledgeDistiller:
    """LLM 知识蒸馏"""

    SYSTEM_PROMPT = """你是一个专业的知识提取助手。你的任务是从视频转录文本中提取核心知识点。

你必须严格按照以下 JSON 格式输出，不要添加任何其他内容：

```json
{
  "title": "视频标题（简洁概括）",
  "one_liner": "一句话核心要点（不超过50字）",
  "key_points": [
    {"timestamp": "MM:SS", "point": "知识点描述"},
    {"timestamp": "MM:SS", "point": "知识点描述"}
  ],
  "summary": "内容摘要（100-200字）",
  "tags": ["标签1", "标签2", "标签3"],
  "action_items": ["可执行的建议1", "可执行的建议2"],
  "references": ["提到的资源或链接"]
}
```

注意：
1. 时间戳必须是 MM:SS 格式
2. 标签不超过5个，要具体有意义
3. key_points 数量控制在3-8个
4. 如果没有明确的可执行建议，action_items 为空数组
5. 如果没有提到具体资源，references 为空数组"""

    USER_PROMPT_TEMPLATE = """视频信息：
- 平台：{platform}
- 时长：{duration}秒
- 作者：{author}

转录文本：
{transcript}

请提取知识点并输出 JSON 格式结果。"""

    def __init__(self, config: AppConfig):
        self.config = config
        self._init_client()

    def _init_client(self):
        """初始化 OpenAI 客户端（兼容 DashScope）"""
        from openai import OpenAI

        self.client = OpenAI(
            api_key=config.dashscope_api_key,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1"
        )
        self.model = config.qwen_model

    def distill(self, video_info: VideoInfo, transcript: TranscriptResult) -> DistilledKnowledge:
        """执行知识蒸馏"""
        logger.info(f"Distilling knowledge with {self.model}")

        # 构建提示
        user_prompt = self.USER_PROMPT_TEMPLATE.format(
            platform=video_info.platform,
            duration=video_info.duration,
            author=video_info.author or "未知",
            transcript=transcript.to_text()[:8000]  # 限制长度
        )

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.3,
                max_tokens=2000,
            )

            content = response.choices[0].message.content
            return self._parse_response(content)

        except Exception as e:
            logger.error(f"LLM distillation error: {e}")
            return self._fallback_distill(video_info, transcript)

    def _parse_response(self, content: str) -> DistilledKnowledge:
        """解析 LLM 响应"""
        # 提取 JSON
        json_match = re.search(r'```json\s*([\s\S]*?)\s*```', content)
        if json_match:
            json_str = json_match.group(1)
        else:
            # 尝试直接解析
            json_str = content

        try:
            data = json.loads(json_str)
            return DistilledKnowledge(
                title=data.get("title", "未命名"),
                one_liner=data.get("one_liner", ""),
                key_points=data.get("key_points", []),
                summary=data.get("summary", ""),
                tags=data.get("tags", []),
                action_items=data.get("action_items", []),
                references=data.get("references", [])
            )
        except json.JSONDecodeError as e:
            logger.error(f"JSON parse error: {e}")
            return DistilledKnowledge(title="解析失败", one_liner="", key_points=[])

    def _fallback_distill(self, video_info: VideoInfo, transcript: TranscriptResult) -> DistilledKnowledge:
        """降级处理：简单的文本截取"""
        return DistilledKnowledge(
            title=video_info.title or "未知标题",
            one_liner="知识蒸馏失败，请查看原文",
            key_points=[{"timestamp": "00:00", "point": transcript.full_text[:200]}],
            summary=transcript.full_text[:500],
            tags=["需人工处理"],
            action_items=[],
            references=[]
        )


# ============================================================================
# Markdown 写入器
# ============================================================================

class MarkdownWriter:
    """Markdown 笔记生成与写入"""

    TEMPLATE = '''---
title: "{title}"
source_url: "{source_url}"
date: {date}
tags: [{tags}]
type: video_distill
asr_source: "{asr_source}"
noise_tags: [{noise_tags}]
---

# {title}

## 💡 一句话核心
{one_liner}

## 🧠 知识点拆解
{key_points}

## 📝 内容摘要
{summary}

{action_items_section}

{references_section}

## 📹 原始信息
- 来源平台：{platform}
- 作者：{author}
- 原始链接：[点击查看]({source_url})

---
> 由知微系统自动生成，建议人工审核后归档
'''

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir

    def write(self, video_info: VideoInfo, transcript: TranscriptResult,
              knowledge: DistilledKnowledge, noise_tags: list[str]) -> Path:
        """生成并写入 Markdown 文件"""
        # 准备内容
        date_str = datetime.now().strftime("%Y-%m-%d")
        tags_str = ", ".join(f'"{tag}"' for tag in knowledge.tags)
        noise_tags_str = ", ".join(f'"{tag}"' for tag in noise_tags)

        # 格式化知识点
        key_points_str = "\n".join(
            f"- **[{kp['timestamp']}]** {kp['point']}"
            for kp in knowledge.key_points
        )

        # 行动建议部分
        action_items_section = ""
        if knowledge.action_items:
            items_str = "\n".join(f"- {item}" for item in knowledge.action_items)
            action_items_section = f"## ✅ 行动建议\n{items_str}"

        # 参考资源部分
        references_section = ""
        if knowledge.references:
            refs_str = "\n".join(f"- {ref}" for ref in knowledge.references)
            references_section = f"## 🔗 参考资料\n{refs_str}"

        # 生成 Markdown
        content = self.TEMPLATE.format(
            title=knowledge.title,
            source_url=video_info.original_url,
            date=date_str,
            tags=tags_str,
            asr_source=transcript.source,
            noise_tags=noise_tags_str,
            one_liner=knowledge.one_liner,
            key_points=key_points_str,
            summary=knowledge.summary,
            action_items_section=action_items_section,
            references_section=references_section,
            platform=video_info.platform,
            author=video_info.author or "未知"
        )

        # 确定文件名
        safe_title = re.sub(r'[<>:"/\\|?*]', '', knowledge.title)[:50]
        filename = f"{date_str}_{safe_title}.md"
        output_path = self.output_dir / filename

        # 写入文件
        output_path.write_text(content, encoding="utf-8")
        logger.info(f"Markdown saved to: {output_path}")

        return output_path


# ============================================================================
# 主程序入口
# ============================================================================

def process_single_video(url: str, config: AppConfig, args, store: ProcessedStore) -> int:
    """
    处理单个视频的完整蒸馏流程

    Returns:
        0: 成功
        1: 失败
        2: 跳过（已处理过）
    """
    resolver = URLResolver()

    # 1. 解析 URL
    logger.info("=" * 50)
    logger.info("Step 1: Resolving URL")
    try:
        video_info = resolver.resolve(url)
    except Exception as e:
        logger.error(f"URL 解析失败: {e}")
        return 1

    logger.info(f"Platform: {video_info.platform}")
    logger.info(f"Resolved URL: {video_info.resolved_url}")

    # 去重检查
    if store.is_processed(video_info.resolved_url) and not getattr(args, 'force', False):
        record = store.get_record(video_info.resolved_url)
        logger.info(f"⏭️ 已处理过，跳过（使用 --force 强制重新处理）")
        if record:
            logger.info(f"   原输出: {record.get('output_path', 'N/A')}")
            logger.info(f"   处理时间: {record.get('processed_at', 'N/A')}")
        return 2

    # 2. 获取转录
    logger.info("=" * 50)
    logger.info("Step 2: Getting transcript")
    cookies_browser = getattr(args, 'cookies_from_browser', None)
    if cookies_browser:
        logger.info(f"Using cookies from browser: {cookies_browser}")
    provider = TranscriptProvider(config, cookies_browser)
    transcript = provider.get_transcript(video_info)

    if not transcript.full_text:
        logger.error("Failed to get transcript")
        return 1

    logger.info(f"Transcript source: {transcript.source}")
    logger.info(f"Transcript length: {len(transcript.full_text)} chars")

    # 只输出转录
    if getattr(args, 'transcript_only', False):
        print("\n" + "=" * 50)
        print("Transcript:")
        print("=" * 50)
        print(transcript.to_text())
        return 0

    # 3. 后处理
    logger.info("=" * 50)
    logger.info("Step 3: Post-processing transcript")
    processor = TranscriptPostProcessor()
    transcript = processor.clean(transcript)
    noise_tags = processor.add_noise_tags(transcript)
    if noise_tags:
        logger.info(f"Detected noise tags: {noise_tags}")

    # 4. 知识蒸馏
    logger.info("=" * 50)
    logger.info("Step 4: Distilling knowledge")
    distiller = KnowledgeDistiller(config)
    knowledge = distiller.distill(video_info, transcript)
    logger.info(f"Title: {knowledge.title}")
    logger.info(f"Key points: {len(knowledge.key_points)}")
    logger.info(f"Tags: {knowledge.tags}")

    # Dry run 模式
    if getattr(args, 'dry_run', False):
        logger.info("=" * 50)
        logger.info("Dry run mode - skipping file generation")
        print(f"\nTitle: {knowledge.title}")
        print(f"One-liner: {knowledge.one_liner}")
        print(f"Summary: {knowledge.summary[:100]}...")
        return 0

    # 5. 生成 Markdown
    logger.info("=" * 50)
    logger.info("Step 5: Writing Markdown")
    writer = MarkdownWriter(config.output_dir)
    output_path = writer.write(video_info, transcript, knowledge, noise_tags)

    # 标记已处理
    store.mark_processed(video_info.resolved_url, output_path, knowledge.title)

    logger.info("=" * 50)
    logger.info(f"✅ Done! Output: {output_path}")
    return 0


def main():
    """CLI 入口"""
    import argparse

    parser = argparse.ArgumentParser(
        description="抖音知识蒸馏引擎 - 从短视频生成 Markdown 笔记",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 从分享文本提取 URL
  python douyin_distiller.py --extract-only --from-text '分享文本...'

  # 从剪贴板处理（macOS）
  pbpaste | python douyin_distiller.py --stdin

  # 完整蒸馏
  python douyin_distiller.py --from-text '分享文本...'

  # 批量处理
  python douyin_distiller.py --input-file shares.txt
        """
    )

    # 输入源参数（互斥）
    input_group = parser.add_mutually_exclusive_group()
    input_group.add_argument("url", nargs="?", help="视频链接（抖音/TikTok/B站等）")
    input_group.add_argument("--from-text", type=str, help="直接处理一段分享文本")
    input_group.add_argument("--stdin", action="store_true", help="从标准输入读取分享文本")
    input_group.add_argument("--input-file", type=str, help="从文件读取，每行一条分享文本")

    # 处理参数
    parser.add_argument("--extract-only", action="store_true", help="仅提取并打印 URL，不执行蒸馏")
    parser.add_argument("--dry-run", action="store_true", help="只解析不生成文件")
    parser.add_argument("--transcript-only", action="store_true", help="只输出转录文本")
    parser.add_argument("--force", action="store_true", help="即使已处理过也强制重新蒸馏")
    parser.add_argument("--output-dir", type=str, help="自定义输出目录")
    parser.add_argument("--cookies-from-browser", type=str, metavar="BROWSER",
                        help="从浏览器加载 cookies（chrome/safari/firefox/edge）")
    parser.add_argument("--debug", action="store_true", help="启用调试模式")

    args = parser.parse_args()

    # 配置日志级别
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    # 确定输入文本
    raw_text = ""
    if args.stdin:
        raw_text = sys.stdin.read().strip()
    elif args.from_text:
        raw_text = args.from_text
    elif args.input_file:
        with open(args.input_file, 'r', encoding='utf-8') as f:
            raw_text = f.read()
    elif args.url:
        raw_text = args.url  # 兼容原有用法

    if not raw_text:
        print("错误：请提供 URL 或使用 --from-text / --stdin / --input-file")
        return 1

    # 提取 URL
    extractor = ShareTextExtractor()
    urls = extractor.extract(raw_text)

    if not urls:
        print("未从输入中检测到视频链接")
        return 1

    print(f"📋 检测到 {len(urls)} 个视频链接")

    # 仅提取模式
    if args.extract_only:
        for url in urls:
            print(f"  → {url}")
        return 0

    # 加载配置
    config = AppConfig()

    # 覆盖输出目录
    if args.output_dir:
        config.output_dir = Path(args.output_dir).expanduser()

    config.ensure_output_dir()

    # 验证配置
    if not args.dry_run and not config.validate():
        logger.error("Configuration validation failed")
        return 1

    # 初始化去重存储
    store = ProcessedStore()

    # 统计处理结果
    results = {"success": 0, "failed": 0, "skipped": 0}

    # 逐个处理
    for i, url in enumerate(urls):
        print(f"\n{'='*50}")
        print(f"[{i+1}/{len(urls)}] {url}")

        result = process_single_video(url, config, args, store)

        if result == 0:
            results["success"] += 1
        elif result == 1:
            results["failed"] += 1
        elif result == 2:
            results["skipped"] += 1

    # 输出统计
    if len(urls) > 1:
        print(f"\n{'='*50}")
        print(f"处理完成: 成功={results['success']}, 失败={results['failed']}, 跳过={results['skipped']}")

    return 0 if results["failed"] == 0 else 1


if __name__ == "__main__":
    exit(main())