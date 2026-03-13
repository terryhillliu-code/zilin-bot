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
        self.asr_model = os.getenv("ASR_MODEL", "sensevoice-v1")
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

    def __init__(self):
        self.yt_dlp_path = self._find_yt_dlp()

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
        """使用 DashScope ASR 转录"""
        if not self._available:
            raise RuntimeError("DashScope API key not configured")

        logger.info(f"Transcribing with DashScope {self.model}: {audio_path}")

        try:
            import dashscope
            from dashscope.audio.asr import Transcription

            dashscope.api_key = self.api_key

            # 根据模型选择参数
            if self.model.startswith("sensevoice"):
                # SenseVoice 模型参数
                task_response = Transcription.async_call(
                    model=self.model,
                    file_urls=[f"file://{audio_path.absolute()}"],
                    language_hints=["zh", "en"],
                )
            else:
                # Paraformer 模型参数
                task_response = Transcription.async_call(
                    model=self.model,
                    file_urls=[f"file://{audio_path.absolute()}"],
                    language_hints=["zh", "en"],
                )

            # 获取结果
            transcription_response = Transcription.fetch(task=task_response)
            if transcription_response.status_code == 200:
                result = transcription_response.output
                return self._parse_dashscope_result(result)

            logger.error(f"DashScope error: {transcription_response.message}")
            return TranscriptResult()

        except ImportError:
            logger.error("dashscope not installed. Run: pip install dashscope")
            return TranscriptResult()
        except Exception as e:
            logger.error(f"DashScope transcription error: {e}")
            return TranscriptResult()

    def _parse_dashscope_result(self, result: dict) -> TranscriptResult:
        """解析 DashScope 返回结果"""
        segments = []
        full_text = ""

        try:
            # 解析结果结构
            if "results" in result:
                for item in result["results"]:
                    if "transcription_url" in item:
                        # 需要下载 JSON 结果
                        response = requests.get(item["transcription_url"], timeout=30)
                        data = response.json()
                    else:
                        data = item

                    # 提取转录文本
                    if "transcripts" in data:
                        for transcript in data["transcripts"]:
                            text = transcript.get("text", "")
                            full_text += text

                            # 提取时间戳（如果有）
                            if "sentences" in transcript:
                                for sentence in transcript["sentences"]:
                                    segments.append(TranscriptSegment(
                                        start=sentence.get("begin_time", 0) / 1000,
                                        end=sentence.get("end_time", 0) / 1000,
                                        text=sentence.get("text", "")
                                    ))

            return TranscriptResult(
                segments=segments,
                full_text=full_text,
                source="dashscope_asr",
                language="zh",
                confidence=0.95
            )

        except Exception as e:
            logger.error(f"Error parsing DashScope result: {e}")
            return TranscriptResult(full_text=full_text, source="dashscope_asr")


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

    def __init__(self, config: AppConfig):
        self.config = config
        self.media_extractor = MediaExtractor()
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

def main():
    """CLI 入口"""
    import argparse

    parser = argparse.ArgumentParser(
        description="抖音知识蒸馏引擎 - 从短视频生成 Markdown 笔记"
    )
    parser.add_argument("url", help="视频链接（抖音/TikTok/B站等）")
    parser.add_argument("--dry-run", action="store_true", help="只解析不生成文件")
    parser.add_argument("--transcript-only", action="store_true", help="只输出转录文本")
    parser.add_argument("--output-dir", type=str, help="自定义输出目录")
    parser.add_argument("--debug", action="store_true", help="启用调试模式")

    args = parser.parse_args()

    # 配置日志级别
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

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

    try:
        # 1. 解析 URL
        logger.info("=" * 50)
        logger.info("Step 1: Resolving URL")
        resolver = URLResolver()
        video_info = resolver.resolve(args.url)
        logger.info(f"Platform: {video_info.platform}")
        logger.info(f"Resolved URL: {video_info.resolved_url}")

        # 2. 获取转录
        logger.info("=" * 50)
        logger.info("Step 2: Getting transcript")
        provider = TranscriptProvider(config)
        transcript = provider.get_transcript(video_info)

        if not transcript.full_text:
            logger.error("Failed to get transcript")
            return 1

        logger.info(f"Transcript source: {transcript.source}")
        logger.info(f"Transcript length: {len(transcript.full_text)} chars")

        # 只输出转录
        if args.transcript_only:
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
        if args.dry_run:
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

        logger.info("=" * 50)
        logger.info(f"✅ Done! Output: {output_path}")
        return 0

    except Exception as e:
        logger.exception(f"Error: {e}")
        return 1


if __name__ == "__main__":
    exit(main())