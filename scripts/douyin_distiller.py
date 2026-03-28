#!/usr/bin/env python3
"""
抖音知识蒸馏引擎 MVP
从抖音/短视频链接自动生成 Obsidian Markdown 笔记

核心流程：
URL → 解析 → 字幕/ASR → RAG 背景增强 → LLM 蒸馏 → Markdown 输出

作者: 知微系统
版本: v1.2.0 (新增 RAG 背景增强)
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
from enum import Enum

import requests
from dotenv import load_dotenv

# 导入统一的 API Key 获取函数
try:
    from zhiwei_common import get_api_key
except ImportError:
    sys.path.insert(0, str(Path.home() / "zhiwei-common"))
    from zhiwei_common import get_api_key

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)


# ============================================================================
# 错误分类
# ============================================================================

class VideoErrorType(Enum):
    """视频处理错误类型"""
    COOKIE_EXPIRED = "cookie_expired"      # Cookie 过期
    NETWORK_ERROR = "network_error"        # 网络问题
    VIDEO_NOT_FOUND = "video_not_found"    # 视频不存在
    VIDEO_PRIVATE = "video_private"        # 私密视频
    ASR_FAILED = "asr_failed"              # 语音识别失败
    LLM_FAILED = "llm_failed"              # LLM 处理失败
    API_ERROR = "api_error"                # API 错误（如 400/500）
    TIMEOUT = "timeout"                    # 超时
    UNKNOWN = "unknown"                    # 未知错误


# 可重试的错误类型
RETRYABLE_ERRORS = [VideoErrorType.NETWORK_ERROR, VideoErrorType.ASR_FAILED, VideoErrorType.TIMEOUT]
MAX_RETRIES = 3


def classify_error(exception: Exception, stderr: str = "") -> tuple[VideoErrorType, str]:
    """根据异常类型和错误输出分类错误

    Args:
        exception: 捕获的异常
        stderr: 命令执行的 stderr 输出

    Returns:
        (错误类型, 错误信息) 元组
    """
    error_str = str(exception).lower()
    stderr_str = stderr.lower() if stderr else ""

    # 合并错误信息用于判断
    combined = error_str + " " + stderr_str

    # Cookie 过期
    if any(kw in combined for kw in ["cookie", "fresh cookies", "cookies expired", "登录过期", "请先登录"]):
        return VideoErrorType.COOKIE_EXPIRED, str(exception) or stderr[:500]

    # 网络错误
    if any(kw in combined for kw in ["network", "connection", "connect", "timeout", "timed out", "网络", "连接"]):
        return VideoErrorType.NETWORK_ERROR, str(exception) or stderr[:500]

    # 超时
    if "timeout" in combined or "timed out" in combined:
        return VideoErrorType.TIMEOUT, str(exception) or stderr[:500]

    # 视频不存在
    if any(kw in combined for kw in ["not found", "404", "视频不存在", "已被删除", "作品不存在"]):
        return VideoErrorType.VIDEO_NOT_FOUND, str(exception) or stderr[:500]

    # 私密视频
    if any(kw in combined for kw in ["private", "私密", "仅自己可见", "私密账号"]):
        return VideoErrorType.VIDEO_PRIVATE, str(exception) or stderr[:500]

    # API 错误
    if any(kw in combined for kw in ["400", "401", "403", "500", "502", "503", "api error", "api请求"]):
        return VideoErrorType.API_ERROR, str(exception) or stderr[:500]

    # ASR 失败
    if any(kw in combined for kw in ["asr", "transcri", "转录", "语音识别"]):
        return VideoErrorType.ASR_FAILED, str(exception) or stderr[:500]

    # LLM 失败
    if any(kw in combined for kw in ["llm", "qwen", "kimi", "distill", "蒸馏"]):
        return VideoErrorType.LLM_FAILED, str(exception) or stderr[:500]

    # 默认未知错误
    return VideoErrorType.UNKNOWN, str(exception) or stderr[:500] or "未知错误"


def should_retry(error_type: VideoErrorType, retry_count: int) -> bool:
    """判断是否应该重试

    Args:
        error_type: 错误类型
        retry_count: 当前重试次数

    Returns:
        True 如果应该重试
    """
    return error_type in RETRYABLE_ERRORS and retry_count < MAX_RETRIES


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
    error_details: str = "" # 新增：记录详细的 ASR 报错信息 (v5.9)

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
    title: str  # 主张式标题
    core_insight: str  # 核心观点（一句话论点）
    content_tier: str = "B"  # 内容质量等级 A/B/C/D
    key_points: list[dict] = field(default_factory=list)  # [{timestamp, insight}]
    summary: str = ""
    target_audience: str = ""  # 目标受众
    use_cases: str = ""  # 适用场景
    tags: list[str] = field(default_factory=list)
    action_items: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    related_concepts: list[str] = field(default_factory=list)  # 可关联知识
    technical_reconstruction: dict = field(default_factory=lambda: {
        "architecture": "未提取",
        "tooling": "未提取",
        "metrics": "未提取",
        "pitfalls": "未提取"
    })
    implementation_guide: dict = field(default_factory=dict)


@dataclass
class ImageFrame:
    """图片帧信息"""
    index: int           # 帧序号
    timestamp: float     # 时间戳（秒）
    path: str           # 临时文件路径
    description: str = ""  # VLM 生成的描述


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
                load_dotenv(env_path, override=True)  # .env 文件覆盖环境变量
                logger.info(f"Loaded config from {env_path}")
                loaded = True

        if not loaded:
            logger.warning("No .env file found, using environment variables")

        # API 配置 - 使用统一的延迟加载
        # ASR 优先使用 DASHSCOPE_API_KEY（百炼 key 对 DashScope ASR 无效）
        self.dashscope_api_key = get_api_key(["DASHSCOPE_API_KEY", "BAILIAN_API_KEY", "CODING_PLAN_API_KEY"]) or ""
        self.qwen_model = os.getenv("QWEN_MODEL", "qwen3.5-plus")
        self.asr_model = os.getenv("ASR_MODEL", "paraformer-realtime-v2")
        self.asr_policy = os.getenv("ASR_POLICY", "auto")
        self.local_asr_model = os.getenv("LOCAL_ASR_MODEL", "small")

        # 输出配置（视频笔记专属目录）
        base_output_dir = os.getenv("OUTPUT_DIR", "~/Documents/ZhiweiVault/70-79_个人笔记_Personal/72_视频笔记_Video-Distill")
        self.output_dir = Path(base_output_dir).expanduser()
        self.assets_dir = self.output_dir / "Assets"

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
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Output directory: {self.output_dir}")
        logger.info(f"Assets directory: {self.assets_dir}")


# ============================================================================
# 分享文本提取器
# ============================================================================

class ShareTextExtractor:
    """从各种格式的分享文本中提取视频 URL"""

    # 支持的 URL 模式（按优先级排序）
    URL_PATTERNS = [
        r'https?://v\.douyin\.com/[A-Za-z0-9_/-]+',           # 抖音短链
        r'https?://www\.douyin\.com/video/\d+',              # 抖音长链
        r'https?://www\.tiktok\.com/@[^/]+/video/\d+',       # TikTok
        r'https?://vm\.tiktok\.com/[A-Za-z0-9]+',            # TikTok 短链
        r'https?://www\.bilibili\.com/video/[A-Za-z0-9]+',   # B站
        r'https?://b23\.tv/[A-Za-z0-9]+',                    # B站短链
        r'https?://xhslink\.com/[A-Za-z0-9/]+',              # 小红书短链
        r'https?://www\.xiaohongshu\.com/explore/[a-f0-9]+', # 小红书长链
        r'https?://v\.kuaishou\.com/[A-Za-z0-9]+',           # 快手
        r'https?://www\.kuaishou\.com/short-video/[A-Za-z0-9]+',
        r'https?://(?:www\.)?youtube\.com/watch\?v=[A-Za-z0-9_-]+', # YouTube
        r'https?://youtu\.be/[A-Za-z0-9_-]+',                      # YouTube Short
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
    """记录已处理的视频，避免重复蒸馏（SQLite 版本）"""

    DEFAULT_DB_PATH = os.path.expanduser("~/zhiwei-bot/data/processed_videos.db")

    # 平台视频 ID 提取模式
    VIDEO_ID_PATTERNS = [
        (r'douyin\.com/video/(\d+)', 'dy'),
        (r'bilibili\.com/video/(BV\w+)', 'bili'),
        (r'tiktok\.com/.*/video/(\d+)', 'tt'),
        (r'xiaohongshu\.com/.*/(\w+)', 'xhs'),
        (r'kuaishou\.com/short-video/(\w+)', 'ks'),
    ]

    def __init__(self, db_path: str = None):
        self.db_path = Path(db_path or self.DEFAULT_DB_PATH).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._migrate_json()

    @classmethod
    def extract_video_id(cls, url: str) -> str:
        """从 URL 提取平台视频 ID"""
        for pattern, prefix in cls.VIDEO_ID_PATTERNS:
            m = re.search(pattern, url)
            if m:
                return f"{prefix}_{m.group(1)}"
        return ""

    def _init_db(self):
        """初始化 SQLite 数据库"""
        import sqlite3
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS processed (
                id INTEGER PRIMARY KEY,
                video_id TEXT UNIQUE,
                resolved_url TEXT UNIQUE,
                title TEXT,
                output_path TEXT,
                processed_at TEXT DEFAULT CURRENT_TIMESTAMP
            )''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_video_id ON processed(video_id)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_resolved_url ON processed(resolved_url)')

    def _migrate_json(self):
        """迁移历史 JSON 数据到 SQLite"""
        json_path = self.db_path.parent / "processed_videos.json"
        if not json_path.exists():
            return

        import sqlite3
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            with sqlite3.connect(self.db_path) as conn:
                for resolved_url, record in data.items():
                    video_id = self.extract_video_id(resolved_url)
                    conn.execute('''INSERT OR IGNORE INTO processed
                        (video_id, resolved_url, title, output_path, processed_at)
                        VALUES (?,?,?,?,?)''',
                        (video_id or None, resolved_url,
                         record.get('title', ''), record.get('output_path', ''),
                         record.get('processed_at', '')))

            # 备份旧 JSON
            json_path.rename(json_path.with_suffix('.json.bak'))
            logger.info(f"Migrated {len(data)} records from JSON to SQLite")
        except Exception as e:
            logger.warning(f"JSON migration failed: {e}")

    def is_processed(self, resolved_url: str, video_id: str = None) -> bool:
        """检查是否已处理（video_id 优先）"""
        import sqlite3
        with sqlite3.connect(self.db_path) as conn:
            if video_id:
                if conn.execute('SELECT 1 FROM processed WHERE video_id=?', (video_id,)).fetchone():
                    return True
            if resolved_url:
                if conn.execute('SELECT 1 FROM processed WHERE resolved_url=?', (resolved_url,)).fetchone():
                    return True
        return False

    def get_record(self, resolved_url: str) -> Optional[dict]:
        """获取已处理记录"""
        import sqlite3
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute('SELECT * FROM processed WHERE resolved_url=?', (resolved_url,)).fetchone()
            return dict(row) if row else None

    def mark_processed(self, resolved_url: str, output_path: str, title: str = "", video_id: str = None):
        """标记已处理"""
        import sqlite3
        vid = video_id or self.extract_video_id(resolved_url)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''INSERT OR REPLACE INTO processed
                (video_id, resolved_url, title, output_path)
                VALUES (?,?,?,?)''',
                (vid or None, resolved_url, title, str(output_path)))
        logger.info(f"Marked as processed: {vid or resolved_url}")

    def get_stats(self) -> dict:
        """获取统计信息"""
        import sqlite3
        with sqlite3.connect(self.db_path) as conn:
            count = conn.execute('SELECT COUNT(*) FROM processed').fetchone()[0]
        return {"total_processed": count}


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
        "youtube": ["youtube.com", "youtu.be"],
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
# 抖音本地 API 客户端
# ============================================================================

class DouyinAPIClient:
    """本地抖音 API 客户端

    使用本地部署的 douyin-api 服务获取视频信息和下载链接
    解决 yt-dlp 直接下载抖音视频遇到的 CDN 403 问题
    """

    def __init__(self, base_url: str = "http://localhost:8680"):
        self.base_url = base_url

    def get_video_data(self, url: str) -> dict:
        """获取视频信息

        Args:
            url: 抖音分享链接（支持短链接和长链接）

        Returns:
            视频信息字典，包含 video、author、desc 等字段

        Raises:
            ValueError: API 调用失败
        """
        api_url = f"{self.base_url}/api/hybrid/video_data"
        try:
            resp = requests.get(
                api_url,
                params={"url": url, "minimal": "false"},
                timeout=30
            )
            resp.raise_for_status()
            data = resp.json()

            if data.get("code") == 200:
                return data.get("data", {})
            raise ValueError(f"API error: code={data.get('code')}, msg={data.get('msg')}")
        except requests.exceptions.RequestException as e:
            raise ValueError(f"API request failed: {e}")

    def get_video_url(self, video_data: dict) -> str:
        """提取视频下载链接

        优先使用无水印链接，降级到有水印链接

        Args:
            video_data: get_video_data() 返回的数据

        Returns:
            视频下载 URL

        Raises:
            ValueError: 未找到可用的视频 URL
        """
        v = video_data.get("video", {})

        # 优先级：无水印下载链接 > 无水印播放链接 > 有水印下载链接 > 有水印播放链接
        for key in ["download_addr", "play_addr"]:
            addr = v.get(key, {})
            url_list = addr.get("url_list", [])
            if url_list:
                return url_list[0]

        raise ValueError("No video URL found in video_data")

    def get_video_info(self, url: str) -> tuple[dict, str]:
        """获取视频信息和下载链接（便捷方法）

        Args:
            url: 抖音分享链接

        Returns:
            (video_data, video_url) 元组
        """
        video_data = self.get_video_data(url)
        video_url = self.get_video_url(video_data)
        return video_data, video_url


# ============================================================================
# 媒体提取器
# ============================================================================

class MediaExtractor:
    """使用 yt-dlp 提取字幕和音频，抖音使用本地 API"""

    def __init__(self, cookies_browser: Optional[str] = None, cookies_file: Optional[str] = None):
        self.yt_dlp_path = self._find_yt_dlp()
        self.cookies_browser = cookies_browser
        self.cookies_file = cookies_file

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
        if self.cookies_file:
            ydl_opts["cookiefile"] = self.cookies_file
        elif self.cookies_browser:
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

    def download_video(self, video_info: VideoInfo, output_path: Path) -> bool:
        """下载视频文件（用于图片视频检测等场景）

        Args:
            video_info: 视频信息
            output_path: 视频输出路径

        Returns:
            是否成功
        """
        # 抖音平台：使用本地 API
        if video_info.platform == "douyin":
            return self._download_douyin_video(video_info, output_path)

        # 其他平台：使用 yt-dlp
        import yt_dlp

        logger.info(f"Downloading video to {output_path}")

        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "format": "best[ext=mp4]/best",
            "outtmpl": str(output_path.with_suffix("")),
        }

        # 添加 cookies 支持
        if self.cookies_file:
            ydl_opts["cookiefile"] = self.cookies_file
        elif self.cookies_browser:
            ydl_opts["cookiesfrombrowser"] = (self.cookies_browser,)

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([video_info.resolved_url])

            # 检查输出文件
            if output_path.exists():
                logger.info(f"Video downloaded: {output_path}")
                return True
            # 尝试其他扩展名
            for ext in [".mp4", ".mkv", ".webm"]:
                alt_path = output_path.with_suffix(ext)
                if alt_path.exists():
                    # 重命名为期望的路径
                    alt_path.rename(output_path)
                    logger.info(f"Video downloaded: {output_path}")
                    return True

            logger.error(f"Video file not found after download")
            return False

        except Exception as e:
            logger.error(f"Error downloading video: {e}")
            return False

    def _download_douyin_video(self, video_info: VideoInfo, output_path: Path) -> bool:
        """下载抖音视频（不提取音频）

        Args:
            video_info: 视频信息
            output_path: 视频输出路径

        Returns:
            是否成功
        """
        try:
            client = DouyinAPIClient()

            # 获取视频信息和下载链接
            logger.info(f"Fetching douyin video info for download: {video_info.original_url}")
            video_data, video_url = client.get_video_info(video_info.original_url)

            # 更新视频信息
            if not video_info.title:
                video_info.title = video_data.get("desc", "")[:100]
            if not video_info.author:
                author_info = video_data.get("author", {})
                video_info.author = author_info.get("nickname", "")

            logger.info(f"Douyin video URL obtained: {video_url[:80]}...")

            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://www.douyin.com/",
                "Accept": "*/*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }

            # 下载视频
            logger.info("Downloading douyin video...")
            resp = requests.get(video_url, headers=headers, timeout=120, stream=True)
            if resp.status_code != 200:
                logger.error(f"Video download failed: HTTP {resp.status_code}")
                return False

            with open(output_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)

            logger.info(f"Video downloaded: {output_path} ({output_path.stat().st_size} bytes)")
            return True

        except Exception as e:
            logger.error(f"Douyin video download error: {e}")
            return False

    def extract_audio(self, video_info: VideoInfo, output_path: Path) -> bool:
        """提取音频文件

        抖音平台：使用本地 douyin-api 服务获取下载链接
        其他平台：使用 yt-dlp 下载

        Raises:
            ValueError: 抖音 API 调用失败时抛出，包含详细错误信息
        """
        # 抖音平台：使用本地 API
        if video_info.platform == "douyin":
            # 不捕获异常，让 API 错误正确传播
            return self._extract_douyin_audio(video_info, output_path)

        # 其他平台：使用 yt-dlp
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
        if self.cookies_file:
            ydl_opts["cookiefile"] = self.cookies_file
        elif self.cookies_browser:
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

    def _extract_douyin_audio(self, video_info: VideoInfo, output_path: Path) -> bool:
        """通过本地 API 下载抖音视频并提取音频

        使用本地部署的 douyin-api 服务获取视频下载链接，
        然后用 ffmpeg 下载并提取音频轨道。

        Args:
            video_info: 视频信息
            output_path: 音频输出路径

        Returns:
            是否成功
        """
        try:
            client = DouyinAPIClient()

            # 获取视频信息和下载链接
            logger.info(f"Fetching douyin video info via local API: {video_info.original_url}")
            video_data, video_url = client.get_video_info(video_info.original_url)

            # 更新视频信息
            if not video_info.title:
                video_info.title = video_data.get("desc", "")[:100]  # 描述可能很长，截取前100字符
            if not video_info.author:
                author_info = video_data.get("author", {})
                video_info.author = author_info.get("nickname", "")

            logger.info(f"Douyin video URL obtained: {video_url[:80]}...")

            # 使用 requests 下载视频（绕过 CDN 防盗链）
            mp3_path = output_path.with_suffix(".mp3")
            video_tmp = output_path.with_suffix(".mp4")

            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://www.douyin.com/",
                "Accept": "*/*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }

            # 下载视频
            logger.info("Downloading video with requests...")
            resp = requests.get(video_url, headers=headers, timeout=60, stream=True)
            if resp.status_code != 200:
                logger.error(f"Video download failed: HTTP {resp.status_code}")
                return False

            with open(video_tmp, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)

            logger.info(f"Video downloaded: {video_tmp} ({video_tmp.stat().st_size} bytes)")

            # 使用 ffmpeg 从本地文件提取音频
            cmd = [
                "ffmpeg", "-y",
                "-i", str(video_tmp),
                "-vn",  # 不包含视频
                "-acodec", "libmp3lame",
                "-q:a", "2",  # 高质量音频
                str(mp3_path)
            ]

            logger.info(f"Running ffmpeg to extract audio...")
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=120  # 2分钟超时
            )

            if result.returncode == 0 and mp3_path.exists():
                logger.info(f"Douyin audio extracted successfully: {mp3_path}")
                # 清理临时视频文件
                if video_tmp.exists():
                    video_tmp.unlink()
                    logger.debug(f"Cleaned up temp video: {video_tmp}")
                return True
            else:
                stderr = result.stderr.decode('utf-8', errors='replace')
                logger.error(f"ffmpeg error (returncode={result.returncode}): {stderr[:500]}")
                # 清理临时文件
                if video_tmp.exists():
                    video_tmp.unlink()
                return False

        except ValueError as e:
            logger.error(f"Douyin API error: {e}")
            # ⭐ 重新抛出异常，让上层能够正确分类和记录错误
            raise
        except subprocess.TimeoutExpired:
            logger.error("ffmpeg timeout (>120s)")
            raise TimeoutError("ffmpeg timeout while extracting audio")
        except Exception as e:
            logger.error(f"Douyin audio extraction failed: {e}")
            raise


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

    def __init__(self, api_key: str, model: str = "paraformer-realtime-v2"):
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
                msg = f"DashScope API Error (Code: {result.status_code}, Msg: {result.message})"
                logger.error(msg)
                return TranscriptResult(error_details=msg)

        except ImportError:
            msg = "dashscope not installed."
            logger.error(msg)
            return TranscriptResult(error_details=msg)
        except Exception as e:
            msg = f"DashScope internal error: {str(e)}"
            logger.error(msg)
            return TranscriptResult(error_details=msg)

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
            msg = f"Error parsing result: {str(e)}"
            logger.error(msg)
            return TranscriptResult(full_text=full_text, source="dashscope_recognition", error_details=msg)

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
# 图片视频处理器
# ============================================================================

class ImageVideoProcessor:
    """
    处理图片/幻灯片类型的视频

    这类视频由连续图片组成，没有实际的动态画面，
    需要通过帧提取 + VLM 识别来获取内容。
    """

    # 图片视频判定阈值
    SCENE_CHANGE_THRESHOLD = 0.15  # 场景变化阈值（低于此值判定为图片视频）
    MIN_FRAME_INTERVAL = 2.0       # 最小帧间隔（秒）
    MAX_FRAMES = 15                # 最大提取帧数

    def __init__(self, config: AppConfig):
        self.config = config
        self._vlm_engine = None

    def _get_vlm_engine(self):
        """延迟初始化 VLM 引擎"""
        if self._vlm_engine is None:
            try:
                # 尝试导入 VLM 引擎
                vlm_path = Path.home() / "zhiwei-rag" / "multimodal"
                try:
                    from multimodal.vlm_engine import VLMEngine
                except ImportError:
                    if str(vlm_path.parent) not in sys.path:
                        sys.path.insert(0, str(vlm_path.parent))
                    from multimodal.vlm_engine import VLMEngine

                self._vlm_engine = VLMEngine(
                    model_name="qwen-vl-plus",
                    prefer_local=False,  # 优先云端，更稳定
                    api_key=self.config.dashscope_api_key
                )
                logger.info("VLM Engine initialized for image video processing")
            except ImportError as e:
                logger.warning(f"VLM Engine not available: {e}")
                self._vlm_engine = None

        return self._vlm_engine

    def is_image_video(self, video_path: Path) -> bool:
        """
        判断是否为图片视频

        通过分析场景变化率和帧相似度来判断：
        - 图片视频：场景变化极少，大部分帧相似
        - 正常视频：场景变化频繁

        Args:
            video_path: 视频文件路径

        Returns:
            True 如果是图片视频
        """
        try:
            # 方法1：使用 ffmpeg 检测场景变化
            cmd = [
                "ffmpeg", "-i", str(video_path),
                "-vf", f"select='gt(scene,{self.SCENE_CHANGE_THRESHOLD})',showinfo",
                "-f", "null", "-"
            ]

            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60
            )

            # 统计场景变化次数
            scene_changes = result.stderr.count("showinfo")

            # 获取视频时长
            duration_cmd = [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "csv=p=0", str(video_path)
            ]
            duration_result = subprocess.run(
                duration_cmd, capture_output=True, text=True, timeout=10
            )

            if duration_result.returncode == 0:
                duration = float(duration_result.stdout.strip())
                # 每分钟场景变化次数
                changes_per_minute = (scene_changes / duration) * 60 if duration > 0 else 0

                # 图片视频通常每分钟场景变化 < 5 次
                is_image = changes_per_minute < 5

                logger.info(
                    f"Video analysis: {scene_changes} scene changes, "
                    f"{changes_per_minute:.1f}/min, is_image_video={is_image}"
                )

                if is_image:
                    return True

            # 方法2：提取几帧检查相似度
            return self._check_frame_similarity(video_path)

        except subprocess.TimeoutExpired:
            logger.warning("Video analysis timeout, assuming regular video")
        except Exception as e:
            logger.warning(f"Video analysis failed: {e}")

        return False

    def _check_frame_similarity(self, video_path: Path) -> bool:
        """检查帧相似度，判断是否为图片视频"""
        try:
            with tempfile.TemporaryDirectory(prefix="framesim_") as tmpdir:
                tmpdir_path = Path(tmpdir)

                # 提取3帧进行对比
                for i, ts in enumerate([0, 2, 5]):
                    frame_path = tmpdir_path / f"frame_{i}.jpg"
                    cmd = [
                        "ffmpeg", "-y", "-ss", str(ts),
                        "-i", str(video_path),
                        "-vframes", "1", "-q:v", "2",
                        str(frame_path)
                    ]
                    subprocess.run(cmd, capture_output=True, timeout=30)

                # 检查提取的帧
                frames = list(tmpdir_path.glob("frame_*.jpg"))
                if len(frames) < 2:
                    return False

                # 使用 ImageHash 计算帧相似度
                try:
                    import imagehash
                    from PIL import Image

                    hashes = []
                    for frame in frames:
                        img = Image.open(frame)
                        h = imagehash.average_hash(img)
                        hashes.append(h)

                    # 计算平均汉明距离
                    if len(hashes) >= 2:
                        total_diff = sum(h1 - h2 for i, h1 in enumerate(hashes) for h2 in hashes[i+1:])
                        avg_diff = total_diff / (len(hashes) * (len(hashes) - 1) / 2)

                        # 平均汉明距离 < 5 表示非常相似（图片视频）
                        is_similar = avg_diff < 5
                        logger.info(f"Frame similarity: avg_diff={avg_diff:.1f}, is_similar={is_similar}")
                        return is_similar

                except ImportError:
                    # imagehash 不可用，使用像素对比
                    from PIL import Image
                    import numpy as np

                    arrays = []
                    for frame in frames:
                        img = Image.open(frame).convert('L').resize((64, 64))
                        arrays.append(np.array(img))

                    if len(arrays) >= 2:
                        # 计算像素差异
                        diffs = [np.mean(np.abs(arrays[i] - arrays[j]))
                                 for i in range(len(arrays)) for j in range(i+1, len(arrays))]
                        avg_diff = sum(diffs) / len(diffs)

                        # 平均像素差异 < 10 表示非常相似
                        is_similar = avg_diff < 10
                        logger.info(f"Frame pixel similarity: avg_diff={avg_diff:.1f}, is_similar={is_similar}")
                        return is_similar

        except Exception as e:
            logger.warning(f"Frame similarity check failed: {e}")

        return False

    def extract_key_frames(
        self,
        video_path: Path,
        output_dir: Path
    ) -> list[ImageFrame]:
        """
        提取关键帧

        Args:
            video_path: 视频文件路径
            output_dir: 输出目录

        Returns:
            ImageFrame 列表
        """
        frames = []

        try:
            # 确保输出目录存在
            output_dir.mkdir(parents=True, exist_ok=True)

            # 获取视频时长
            duration_cmd = [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "csv=p=0", str(video_path)
            ]
            result = subprocess.run(
                duration_cmd, capture_output=True, text=True, timeout=10
            )

            if result.returncode != 0:
                logger.error("Failed to get video duration")
                return frames

            duration = float(result.stdout.strip())

            # 计算帧间隔
            frame_count = min(
                int(duration / self.MIN_FRAME_INTERVAL) + 1,
                self.MAX_FRAMES
            )
            interval = duration / frame_count if frame_count > 0 else self.MIN_FRAME_INTERVAL

            logger.info(f"Extracting {frame_count} frames from {duration:.1f}s video")

            # 提取帧
            for i in range(frame_count):
                timestamp = i * interval
                frame_path = output_dir / f"frame_{i:03d}.jpg"

                cmd = [
                    "ffmpeg", "-y", "-ss", str(timestamp),
                    "-i", str(video_path),
                    "-vframes", "1",
                    "-q:v", "2",
                    str(frame_path)
                ]

                result = subprocess.run(
                    cmd, capture_output=True, timeout=30
                )

                if result.returncode == 0 and frame_path.exists():
                    frames.append(ImageFrame(
                        index=i,
                        timestamp=timestamp,
                        path=str(frame_path)
                    ))
                else:
                    logger.warning(f"Failed to extract frame at {timestamp:.1f}s")

            logger.info(f"Successfully extracted {len(frames)} frames")

        except Exception as e:
            logger.error(f"Frame extraction failed: {e}")

        return frames

    def describe_frames(
        self,
        frames: list[ImageFrame],
        prompt: Optional[str] = None
    ) -> list[ImageFrame]:
        """
        使用 VLM 描述每帧内容

        Args:
            frames: 帧列表
            prompt: 自定义提示词

        Returns:
            更新了 description 的帧列表
        """
        vlm = self._get_vlm_engine()

        if vlm is None:
            logger.error("VLM Engine not available")
            # 回退：直接标记无法处理
            for frame in frames:
                frame.description = "[VLM 不可用，无法识别图片内容]"
            return frames

        # 方案B: VLM提取增强 - 结构化知识提取
        default_prompt = """你是一个知识提取专家。请分析这张图片并提取结构化知识。

## 输出格式要求

### 1. 图片类型识别
[选择一项：信息图 / 流程图 / 思维导图 / 数据图表 / 代码截图 / 文字截图 / 幻灯片 / 照片 / 其他]

### 2. 完整文字提取（OCR）
请尽可能提取图片中的所有可见文字，包括：
- 标题、副标题
- 正文内容
- 标注、注释
- 图例说明

### 3. 核心知识点
用要点形式列出图片传达的核心知识：
- 要点1：...
- 要点2：...
- ...

### 4. 结构化信息
如果是图表/流程图，请描述其结构：
- 输入 → 处理 → 输出的流程
- 层级关系
- 数据对比

### 5. 关键数据
提取所有数值、比例、时间等关键数据。

请确保提取的信息完整、准确，便于后续知识蒸馏。"""

        actual_prompt = prompt or default_prompt

        for frame in frames:
            try:
                logger.info(f"Describing frame {frame.index} at {frame.timestamp:.1f}s")
                result = vlm.describe_image(
                    frame.path,
                    prompt=actual_prompt,
                    max_tokens=500
                )
                frame.description = result.description
                logger.info(f"Frame {frame.index}: {result.description[:100]}...")

            except Exception as e:
                logger.error(f"Failed to describe frame {frame.index}: {e}")
                frame.description = f"[图片识别失败: {e}]"

        return frames

    def synthesize_transcript(
        self,
        frames: list[ImageFrame]
    ) -> TranscriptResult:
        """
        将帧描述合称为转录结果

        方案C: 添加低信息密度检测

        Args:
            frames: 包含描述的帧列表

        Returns:
            TranscriptResult
        """
        segments = []
        full_text_parts = []

        # 统计有效帧数
        valid_frames = [f for f in frames if f.description and not f.description.startswith("[")]
        frame_count = len(valid_frames)

        # 方案C: 添加图片内容标记
        header = f"""[📷 图片内容提取 - 共{frame_count}张图片]

---
"""
        full_text_parts.append(header)

        for frame in frames:
            if frame.description and not frame.description.startswith("["):
                # 创建转录片段
                segment = TranscriptSegment(
                    start=frame.timestamp,
                    end=frame.timestamp + self.MIN_FRAME_INTERVAL,
                    text=frame.description
                )
                segments.append(segment)
                full_text_parts.append(f"### 图片 {frame.index + 1}\n\n{frame.description}")

        full_text = "\n\n---\n\n".join(full_text_parts)

        # 方案C: 低信息密度检测
        total_chars = len(full_text)
        avg_chars_per_frame = total_chars // max(frame_count, 1)

        # 检测低信息密度：平均每张图片 < 200 字符
        is_low_density = avg_chars_per_frame < 200 and frame_count > 0

        if is_low_density:
            warning = f"""

---

⚠️ **低信息密度警告**
- 图片数量: {frame_count}
- 总字符数: {total_chars}
- 平均每张: {avg_chars_per_frame} 字符

该图文内容信息密度较低，可能为纯视觉内容或信息较少，建议人工审核。
"""
            full_text += warning
            logger.warning(f"Low information density detected: {avg_chars_per_frame} chars/frame")

        return TranscriptResult(
            segments=segments,
            full_text=full_text,
            source="vlm_image_frames",
            language="zh",
            confidence=0.85
        )

    def _format_time(self, seconds: float) -> str:
        """格式化时间戳"""
        mins = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{mins:02d}:{secs:02d}"

    def process_image_video(
        self,
        video_info: VideoInfo,
        video_path: Path
    ) -> TranscriptResult:
        """
        处理图片视频的完整流程

        Args:
            video_info: 视频信息
            video_path: 视频文件路径

        Returns:
            TranscriptResult
        """
        logger.info(f"Processing image video: {video_info.title}")

        with tempfile.TemporaryDirectory(prefix="imgvid_") as tmpdir:
            tmpdir_path = Path(tmpdir)

            # 提取关键帧
            frames = self.extract_key_frames(video_path, tmpdir_path)

            if not frames:
                logger.error("No frames extracted")
                return TranscriptResult()

            # 使用 VLM 描述帧
            frames = self.describe_frames(frames)

            # 合成为转录结果
            transcript = self.synthesize_transcript(frames)

            logger.info(f"Image video processing complete: {len(transcript.full_text)} chars")

            return transcript


# ============================================================================
# 转录提供者（路由编排）
# ============================================================================

class TranscriptProvider:
    """转录服务路由"""

    def __init__(self, config: AppConfig, cookies_browser: Optional[str] = None, cookies_file: Optional[str] = None):
        self.config = config
        self.cookies_browser = cookies_browser
        self.cookies_file = cookies_file
        self.media_extractor = MediaExtractor(cookies_browser, cookies_file)
        self.dashscope_transcriber = DashScopeASRTranscriber(
            config.dashscope_api_key,
            config.asr_model
        )
        self.local_transcriber = LocalMLXWhisperTranscriber(config.local_asr_model)
        self.image_video_processor = ImageVideoProcessor(config)

    def get_transcript(self, video_info: VideoInfo, save_audio_path: Optional[Path] = None) -> TranscriptResult:
        """获取转录文本，按策略选择方法"""
        policy = self.config.asr_policy

        # 策略：auto - 优先尝试字幕
        if policy == "auto":
            # 尝试提取平台字幕
            subtitle_result = self.media_extractor.extract_subtitles(video_info)
            if subtitle_result and subtitle_result.full_text:
                logger.info("Successfully extracted platform subtitles")
                return subtitle_result

            logger.info("No subtitles found, checking video type...")

        # 检测并处理图片视频
        image_result = self._try_image_video(video_info)
        if image_result and image_result.full_text:
            logger.info("Successfully processed as image video")
            return image_result

        # 普通 ASR 转录
        return self._transcribe_with_asr(video_info, save_audio_path)

    def _try_image_video(self, video_info: VideoInfo) -> Optional[TranscriptResult]:
        """尝试作为图片视频处理"""
        with tempfile.TemporaryDirectory(prefix="vidcheck_") as tmpdir:
            tmpdir_path = Path(tmpdir)
            video_path = tmpdir_path / "video.mp4"

            # 下载视频
            if not self.media_extractor.download_video(video_info, video_path):
                logger.warning("Failed to download video for type check")
                return None

            # 检测是否为图片视频
            if self.image_video_processor.is_image_video(video_path):
                logger.info("Detected image/slide video, using VLM processing")
                return self.image_video_processor.process_image_video(video_info, video_path)

            return None

    def _transcribe_with_asr(self, video_info: VideoInfo, save_audio_path: Optional[Path] = None) -> TranscriptResult:
        """使用 ASR 转录 - 带自动降级"""
        with tempfile.TemporaryDirectory(prefix="distill_") as tmpdir:
            tmpdir_path = Path(tmpdir)

            # 如果提供了持久化路径，直接使用；否则使用临时路径
            audio_path = save_audio_path or (tmpdir_path / "audio")

            # 提取音频
            if not self.media_extractor.extract_audio(video_info, audio_path):
                logger.error("Failed to extract audio")
                return TranscriptResult()

            # 确保使用带后缀的路径进行识别 (MediaExtractor.extract_audio 会自动补全 .mp3)
            actual_audio_path = audio_path.with_suffix(".mp3")

            # 策略：优先云端，失败自动降级到本地
            transcript_result = None

            # 1. 尝试 DashScope ASR
            if self.dashscope_transcriber.is_available():
                logger.info("尝试 DashScope ASR...")
                try:
                    transcript_result = self.dashscope_transcriber.transcribe(actual_audio_path)
                    if transcript_result and transcript_result.full_text:
                        logger.info(f"DashScope ASR 成功: {len(transcript_result.full_text)} 字符")
                        return transcript_result
                    logger.warning("DashScope ASR 返回空结果，尝试降级")
                except Exception as e:
                    logger.warning(f"DashScope ASR 失败: {e}，尝试降级到本地 Whisper")

            # 2. 降级到本地 MLX Whisper
            if self.local_transcriber.is_available():
                logger.info("使用本地 MLX Whisper ASR（降级模式）")
                try:
                    transcript_result = self.local_transcriber.transcribe(actual_audio_path)
                    if transcript_result and transcript_result.full_text:
                        logger.info(f"本地 Whisper ASR 成功: {len(transcript_result.full_text)} 字符")
                        return transcript_result
                    logger.warning("本地 Whisper ASR 返回空结果")
                except Exception as e:
                    logger.error(f"本地 Whisper ASR 也失败: {e}")

            # 3. 无可用服务
            logger.error("No ASR service available (云端和本地都失败)")
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
# RAG 背景增强 (v1.2.0 新增)
# ============================================================================

def extract_keywords_from_transcript(transcript: str, top_n: int = 5) -> list[str]:
    """
    从转录文本中提取核心关键词

    策略：提取技术名词、产品名、专有名词

    Args:
        transcript: 转录文本
        top_n: 返回关键词数量

    Returns:
        关键词列表
    """
    import re

    # 技术名词模式（大写开头、包含数字版本号、英文术语）
    tech_patterns = [
        r'[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*',  # CamelCase
        r'[A-Z]{2,}',  # 缩写
        r'\b[A-Za-z]+\d+(?:\.\d+)*\b',  # 带版本号 GPT-4, LLaMA2
        r'[\u4e00-\u9fa5]{2,}(?:模型|框架|算法|架构|网络|引擎)',  # 中文技术词
    ]

    keywords = []
    for pattern in tech_patterns:
        matches = re.findall(pattern, transcript)
        keywords.extend(matches)

    # 统计频率并返回 top_n
    from collections import Counter
    counter = Counter(keywords)

    # 过滤常见词
    stopwords = {'的', '了', '是', '在', '我', '我们', '这个', '那个', '就是', '可以'}
    filtered = [(k, c) for k, c in counter.most_common(20)
                if k.lower() not in stopwords and len(k) > 1]

    return [k for k, _ in filtered[:top_n]]


def retrieve_background_knowledge(keywords: list[str], top_k: int = 3) -> str:
    """
    从 zhiwei-rag 检索背景知识

    Args:
        keywords: 关键词列表
        top_k: 每个关键词检索数量

    Returns:
        格式化的背景知识文本
    """
    if not keywords:
        return ""

    try:
        # 动态导入避免循环依赖
        rag_path = Path.home() / "zhiwei-rag"
        try:
            from retrieve.hybrid_retriever import HybridRetriever
        except ImportError:
            if str(rag_path) not in sys.path:
                sys.path.insert(0, str(rag_path))
            from retrieve.hybrid_retriever import HybridRetriever

        retriever = HybridRetriever()
        background_parts = []

        for kw in keywords[:3]:  # 最多检索 3 个关键词
            results = retriever.search(kw, top_k=top_k, use_rerank=False)

            if results:
                background_parts.append(f"**{kw}** 相关资料：")
                for r in results[:2]:  # 每个关键词取前 2 条
                    source = Path(r.source).stem if r.source else "未知来源"
                    text = (r.text or r.raw_text or "")[:200]
                    background_parts.append(f"  - {source}: {text}...")
                background_parts.append("")

        if background_parts:
            logger.info(f"[RAG] 检索到 {len(keywords)} 个关键词的背景知识")
            return "\n".join(background_parts)

    except Exception as e:
        logger.warning(f"[RAG] 背景检索失败: {e}")

    return ""


# ============================================================================
# 知识蒸馏器
# ============================================================================

class KnowledgeDistiller:
    """
    知识蒸馏引擎

    使用统一 LLM 客户端 (llm_client.py) 进行知识提取
    默认使用 qwen3.5-plus 模型，自动降级到 glm-5 → MiniMax-M2.5
    """

    SYSTEM_PROMPT = """你是一个顶级的技术研究员与情报分析师，擅长从视频内容中“脱水”出极高密度的技术情报。
你的目标是：**拒绝泛泛而谈的摘要，追求能够直接指导工程实现的技术重建。**

**第一步：评估内容质量等级（量化标准）**

| 等级 | 信息密度 | 核心价值 | 判断标准 |
|------|----------|----------|----------|
| A级 | 极大 | 能够闭环复现 | 包含详尽的技术路线、算法细节、工具链、对比数据和明确的避坑指南 |
| B级 | 较大 | 关键点明确 | 介绍具体工具或方法，有清晰的逻辑链路，但部分细节需查阅文档 |
| C级 | 一般 | 启发/快报性 | 只有概括性介绍，无代码细节或深度原理，仅供跟踪动态 |
| D级 | 极低 | 无实质内容 | 纯广告、泛娱乐重复内容、或与技术/知识无关 |

**第二步：技术情报重建（核心要求）**

请**务必且强制**在 `technical_reconstruction` 字段中提供以下详尽信息（严禁省略此字段）：
1. **architecture**（架构/原理分析）：底层逻辑（如：自回归模型、向量检索等）。
2. **tooling**（工具链细节）：工具、模型、库、API 版本、环境变量等。
3. **metrics**（关键数据指标）：性能提升、成本降低、准确率等具体数值。
4. **pitfalls**（实施细节/避坑指南）：注意点、常见的坑、方案对比。

**第三步：输出 JSON 结构**

```json
{
  "title": "深度情报：[核心点] + [痛点]",
  "core_insight": "一句话神谕",
  "content_tier": "A/B/C/D",
  "key_points": [{"timestamp": "MM:SS", "insight": "技术锚点"}],
  "technical_reconstruction": {
    "architecture": "架构分析",
    "tooling": "工具清单",
    "metrics": "指标数据",
    "pitfalls": "避坑指南"
  },
  "summary": "150-300字深度总结",
  "action_items": ["具体工程建议"],
  "implementation_guide": {
    "prerequisites": "环境依赖",
    "steps": ["复现路径"],
    "difficulty": "难度",
    "key_code_logic": "核心逻辑/代码"
  },
  "tags": ["标签"],
  "related_concepts": ["关联知识"]
}
```

**摘要模板（根据 content_tier 选择）：**

**A级（深度干货）- 150-200字**：
结构：[核心技术原理/方法论一句话] + [具体应用场景，2-3个] + [适用人群+前提条件] + [与其他方案的对比优势]
示例："该项目采用纯前端DOM操作实现AI自动化，无需浏览器插件或后端服务。适用于需要降低用户使用门槛的B端产品、希望提升可访问性的政务系统、以及追求轻量部署的SaaS服务。开发者只需一行代码即可集成，相比Puppeteer等传统方案部署成本降低90%。"

**B级（有价值介绍）- 100-150字**：
结构：[产品/工具核心价值] + [适用场景] + [与替代品差异]
示例："AI界面生成工具，输入自然语言即可一秒生成完整App界面。适用于产品经理快速验证想法、设计师制作原型、创业者展示概念。相比Figma手绘，速度提升10倍；相比传统开发，门槛降至零代码。"

**C级（浅层内容）- 50-80字**：
结构：[核心价值点] + [适合谁]
示例："介绍了某AI工具的基本功能，适合对该领域完全不了解的新手快速建立认知。内容较浅，建议结合官方文档深入学习。"

**第三步：复现与实施指南（重点，用于指导用户落地项目）**

在 JSON 的 `implementation_guide` 字段中输出：
- `prerequisites`: 核心依赖工具/环境（如：Node.js, Python 3.10+, Docker）
- `steps`: 极简复现步骤（1. 2. 3. ...）
- `difficulty`: 复现难度 (简单/中等/困难)
- `key_code_logic`: 提取视频中提到的最核心代码逻辑、API 调用方式或关键伪代码。

**注意：** 旨在帮助用户能够根据这份笔记，在不回看视频的情况下，快速搭建起原型或尝试相关工具。

**重要原则：**
1. **主张式标题**：标题是观点/价值主张，非描述
   - ❌ "AI工具介绍" → ✅ "一行代码让网站支持AI操控"

2. **洞察式知识点**：说明为什么重要、如何应用
   - ❌ "项目有5.3K star" → ✅ "快速获认可说明前端AI化是刚需"

3. **价值导向摘要**：不是"讲了什么"，而是"能得到什么"

4. 时间戳从转录文本推断，格式 MM:SS
5. 标签3-5个，具体有意义
6. key_points 数量：A级5-6个，B级3-5个，C级2-3个，D级1个"""

    USER_PROMPT_TEMPLATE = """视频信息：
- 平台：{platform}
- 时长：{duration}秒
- 作者：{author}

转录文本：
{transcript}

{background_section}

请提取知识点，并特别关注 **“如何复现视频中的方案”**。
输出 JSON 格式结果。"""

    def __init__(self, config: AppConfig):
        self.config = config
        self._init_client()

    def _init_client(self):
        """初始化统一 LLM 客户端"""
        # 导入统一客户端
        try:
            from llm_client import llm_client
        except ImportError:
            sys.path.insert(0, str(Path(__file__).parent.parent))
            from llm_client import llm_client

        self.llm_client = llm_client
        logger.info("Using unified LLM client with distill role (qwen3.5-plus)")

    def distill(self, video_info: VideoInfo, transcript: TranscriptResult) -> DistilledKnowledge:
        """执行知识蒸馏（使用统一客户端自动降级）

        v1.2.0 新增：RAG 背景增强
        """
        logger.info("Distilling knowledge with distill role (qwen3.5-plus + auto-fallback)")

        # v1.2.0: RAG 背景增强
        transcript_text = transcript.to_text()[:8000]  # 限制长度
        background_section = ""

        try:
            # 提取关键词
            keywords = extract_keywords_from_transcript(transcript_text)
            if keywords:
                logger.info(f"[RAG] 提取关键词: {keywords}")
                # 检索背景知识
                background_knowledge = retrieve_background_knowledge(keywords)
                if background_knowledge:
                    background_section = f"**背景知识（来自本地知识库）：**\n{background_knowledge}"
        except Exception as e:
            logger.warning(f"[RAG] 背景增强失败，继续无背景处理: {e}")

        # 构建提示
        user_prompt = self.USER_PROMPT_TEMPLATE.format(
            platform=video_info.platform,
            duration=video_info.duration,
            author=video_info.author or "未知",
            transcript=transcript_text,
            background_section=background_section
        )

        try:
            # 使用统一客户端调用（自动降级）
            success, content = self.llm_client.call(
                role="distill",
                message=user_prompt,
                system_prompt=self.SYSTEM_PROMPT,
                timeout=120
            )

            if success:
                return self._parse_response(content)
            else:
                logger.error(f"LLM distillation failed: {content}")
                return self._fallback_distill(video_info, transcript)

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
                core_insight=data.get("core_insight", data.get("one_liner", "")),
                content_tier=data.get("content_tier", "B"),
                key_points=data.get("key_points", []),
                summary=data.get("summary", ""),
                target_audience=data.get("target_audience", ""),
                use_cases=data.get("use_cases", ""),
                tags=data.get("tags", []),
                action_items=data.get("action_items", []),
                references=data.get("references", []),
                related_concepts=data.get("related_concepts", []),
                technical_reconstruction=data.get("technical_reconstruction", {
                    "architecture": "未提取",
                    "tooling": "未提取",
                    "metrics": "未提取",
                    "pitfalls": "未提取"
                }),
                implementation_guide=data.get("implementation_guide", {})
            )
        except json.JSONDecodeError as e:
            logger.error(f"JSON parse error: {e}")
            return DistilledKnowledge(
                title="解析失败",
                core_insight="",
                content_tier="D",
                key_points=[],
                summary="",
                target_audience="",
                use_cases="",
                tags=["需人工处理"],
                action_items=[],
                references=[],
                related_concepts=[]
            )

    def _fallback_distill(self, video_info: VideoInfo, transcript: TranscriptResult) -> DistilledKnowledge:
        """降级处理：简单的文本截取"""
        return DistilledKnowledge(
            title=video_info.title or "未知标题",
            core_insight="知识蒸馏失败，请查看原文",
            content_tier="D",
            key_points=[{"timestamp": "00:00", "insight": transcript.full_text[:200]}],
            summary=transcript.full_text[:500],
            target_audience="",
            use_cases="",
            tags=["需人工处理"],
            action_items=[],
            references=[],
            related_concepts=[]
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
tier: {content_tier}
asr_source: "{asr_source}"
related: [{related_concepts}]
---

# {title}

> **内容等级：{tier_display}** | **技术重建完成度：高**

## 💡 核心深度洞察
{core_insight}

## 🏗️ 技术情报重建 (Technical Reconstruction)
### 架构与原理
{tech_arch}

### 工具链详情
{tech_tools}

### 量化指标与数据
{tech_metrics}

### 实施细节与坑位
{tech_pitfalls}

## 🧠 关键技术锚点
{key_points}

## 📝 深度技术摘要 (Intel-Brief)
{summary}

## ✅ 具体工程建议 (Action Items)
{action_items_section}

## 🛠️ 研发/复现指南
- **难度等级**：{impl_difficulty}
- **环境依赖**：{impl_prerequisites}
- **核心逻辑/代码**：
```text
{impl_code_logic}
```
- **复现路径**：
{impl_steps}

{references_section}

## 📂 关联资产
- 🎙️ 原始音频：[点击播放]({asset_audio_url})
- 📄 转录简报：[查看摘要]({asset_transcript_url})

---

## 📹 原始信息
- 来源平台：{platform}
- 作者：{author}
- 原始链接：[点击查看]({source_url})

---

## 📜 详细 ASR 转录文本 (完整保留)
<details>
<summary>点击展开 100% 原始转录细节（防止信息丢失）</summary>

{full_transcript}

</details>

---
> 由知微系统生成，建议关联已有知识并添加个人洞察。
> **RAG 隔离标记**：`rag: false` (默认不入库，移动至 Knowledge_Base 目录可入库)
'''

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir

    def write(self, video_info: VideoInfo, transcript: TranscriptResult,
              knowledge: DistilledKnowledge, noise_tags: list[str]) -> Path:
        """生成并写入 Markdown 文件，同时归档资产"""
        # 准备内容
        date_str = datetime.now().strftime("%Y-%m-%d")
        tags_str = ", ".join(f'"{tag}"' for tag in knowledge.tags)
        
        # 确定文件名和资产目录
        safe_title = re.sub(r'[<>:"/\\|?*]', '', knowledge.title)[:50]
        asset_folder_name = f"{date_str}_{safe_title}"
        asset_dir = self.output_dir / "Assets" / asset_folder_name
        asset_dir.mkdir(parents=True, exist_ok=True)

        # 归档转录全文
        transcript_filename = "transcript_full.txt"
        transcript_path = asset_dir / transcript_filename
        transcript_path.write_text(transcript.to_text(), encoding="utf-8")
        
        # 归档音频 (如果有临时文件路径，需要传入。这里假设 transcript 对象或 video_info 携带了 path)
        # 注意：实际音频提取在 TranscriptProvider._transcribe_with_asr 中，
        # 为了简单，我们在这里通过 link 协议引用，具体物理移动需在 distiller 主流程配合。
        asset_audio_url = f"Assets/{asset_folder_name}/audio.mp3"
        asset_transcript_url = f"Assets/{asset_folder_name}/{transcript_filename}"

        # 内容等级显示
        tier_labels = {"A": "⭐⭐⭐ 深度干货", "B": "⭐⭐ 有价值", "C": "⭐ 浅层内容", "D": "⚠️ 信息稀薄"}
        tier_display = tier_labels.get(knowledge.content_tier, "未评估")

        # 实施指南数据
        impl = getattr(knowledge, 'implementation_guide', {})
        impl_difficulty = impl.get('difficulty', '未知')
        impl_prerequisites = impl.get('prerequisites', '见摘要')
        impl_code_logic = impl.get('key_code_logic', '# 提取中...')
        impl_steps = "\n".join([f"{i+1}. {s}" for i, s in enumerate(impl.get('steps', ['参考摘要说明']))])

        # 格式化知识点
        key_points_str = "\n".join(f"- **[{kp['timestamp']}]** {kp['insight']}" for kp in knowledge.key_points)

        # 适用场景
        use_cases_section = f"## 🎯 适用场景\n{knowledge.use_cases}" if knowledge.use_cases else ""

        # 知识关联
        related_section = ", ".join(f"[[{c}]]" for c in knowledge.related_concepts) if knowledge.related_concepts else "暂无关联"

        # 行动建议
        items_str = "\n".join(f"- [ ] {item}" for item in knowledge.action_items) if knowledge.action_items else "- [ ] 思考如何将此知识应用到实际场景"

        # 参考资源
        refs_str = "\n".join(f"- {ref}" for ref in knowledge.references) if knowledge.references else ""
        references_section = f"## 📚 参考资料\n{refs_str}" if refs_str else ""

        # 生成 Markdown
        content = self.TEMPLATE.format(
            title=knowledge.title,
            source_url=video_info.original_url,
            date=date_str,
            tags=tags_str,
            content_tier=knowledge.content_tier,
            tier_display=tier_display,
            target_audience=knowledge.target_audience or "通用",
            asr_source=transcript.source,
            related_concepts=", ".join(f'"{c}"' for c in knowledge.related_concepts) if knowledge.related_concepts else "",
            core_insight=knowledge.core_insight,
            key_points=key_points_str,
            summary=knowledge.summary,
            use_cases_section=use_cases_section,
            related_section=related_section,
            action_items_section=items_str,
            references_section=references_section,
            platform=video_info.platform,
            author=video_info.author or "未知",
            impl_difficulty=impl_difficulty,
            impl_prerequisites=impl_prerequisites,
            impl_code_logic=impl_code_logic,
            impl_steps=impl_steps,
            asset_audio_url=asset_audio_url,
            asset_transcript_url=asset_transcript_url,
            tech_arch=knowledge.technical_reconstruction.get("architecture", "未提取"),
            tech_tools=knowledge.technical_reconstruction.get("tooling", "未提取"),
            tech_metrics=knowledge.technical_reconstruction.get("metrics", "未提取"),
            tech_pitfalls=knowledge.technical_reconstruction.get("pitfalls", "未提取"),
            full_transcript=transcript.to_text()
        )

        filename = f"{date_str}_{safe_title}.md"
        output_path = self.output_dir / filename
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

    失败时输出 JSON 格式错误信息到 stderr:
    {"error_type": "xxx", "error_message": "xxx"}
    """
    resolver = URLResolver()

    # 1. 解析 URL
    logger.info("=" * 50)
    logger.info("Step 1: Resolving URL")
    try:
        video_info = resolver.resolve(url)
    except Exception as e:
        error_type, error_msg = classify_error(e)
        error_json = json.dumps({"error_type": error_type.value, "error_message": error_msg})
        print(error_json, file=sys.stderr)
        logger.error(f"URL 解析失败: {e}")
        return 1

    logger.info(f"Platform: {video_info.platform}")
    logger.info(f"Resolved URL: {video_info.resolved_url}")

    # 去重检查（video_id 优先）
    video_id = ProcessedStore.extract_video_id(video_info.resolved_url)
    if video_id:
        logger.info(f"Video ID: {video_id}")
    if store.is_processed(video_info.resolved_url, video_id=video_id) and not getattr(args, 'force', False):
        record = store.get_record(video_info.resolved_url)
        logger.info(f"⏭️ 已处理过，跳过（使用 --force 强制重新处理）")
        if record:
            output_path = record.get('output_path', 'N/A')
            logger.info(f"   原输出: {output_path}")
            logger.info(f"   处理时间: {record.get('processed_at', 'N/A')}")
            print(f"✅ Done! Output: {output_path}")  # 跳过也输出成功标志
        return 2

    # 2. 获取转录
    logger.info("=" * 50)
    logger.info("Step 2: Getting transcript")
    cookies_browser = getattr(args, 'cookies_from_browser', None)
    cookies_file = getattr(args, 'cookies', None)
    if cookies_browser:
        logger.info(f"Using cookies from browser: {cookies_browser}")
    if cookies_file:
        logger.info(f"Using cookies file: {cookies_file}")

    try:
        provider = TranscriptProvider(config, cookies_browser, cookies_file)
        
        # 预估资产保存路径（基于 URL 解析出的 Title，如果没有则生成 ID）
        date_str = datetime.now().strftime("%Y-%m-%d")
        raw_title = video_info.title or f"Video_{video_id or 'unknown'}"
        safe_title = re.sub(r'[<>:"/\\|?*]', '', raw_title)[:50]
        asset_dir = config.output_dir / "Assets" / f"{date_str}_{safe_title}"
        asset_dir.mkdir(parents=True, exist_ok=True)
        save_audio_path = asset_dir / "audio" # 最终后缀由 extract_audio 补全
        
        transcript = provider.get_transcript(video_info, save_audio_path=save_audio_path)
    except Exception as e:
        error_type, error_msg = classify_error(e)
        error_json = json.dumps({"error_type": error_type.value, "error_message": error_msg})
        print(error_json, file=sys.stderr)
        logger.error(f"获取转录失败: {e}")
        return 1

    if not transcript.full_text:
        # 优先使用透传的详细报错
        err_msg = transcript.error_details or "无法获取转录文本 (ASR 返回空或识别失败)"
        error_json = json.dumps({"error_type": VideoErrorType.ASR_FAILED.value, "error_message": err_msg})
        print(error_json, file=sys.stderr)
        logger.error(f"Failed to get transcript: {err_msg}")
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
    try:
        knowledge = distiller.distill(video_info, transcript)
    except Exception as e:
        error_type, error_msg = classify_error(e)
        error_json = json.dumps({"error_type": error_type.value, "error_message": error_msg})
        print(error_json, file=sys.stderr)
        logger.error(f"知识蒸馏失败: {e}")
        return 1

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
    store.mark_processed(video_info.resolved_url, output_path, knowledge.title, video_id=video_id)

    logger.info("=" * 50)
    logger.info(f"✅ Done! Output: {output_path}")
    print(f"✅ Done! Output: {output_path}")  # 同时输出到 stdout，供调用方检测
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
    parser.add_argument("--cookies", type=str, metavar="FILE",
                        help="cookies 文件路径（Netscape 格式）")
    parser.add_argument("--debug", action="store_true", help="启用调试模式")
    parser.add_argument("--openclaw-payload", type=str, help="OpenClaw 消息 payload（JSON 或纯文本）")
    parser.add_argument("--json", action="store_true", help="JSON 格式输出结果")

    args = parser.parse_args()

    # 配置日志级别
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    # 处理 OpenClaw payload
    if getattr(args, 'openclaw_payload', None):
        try:
            data = json.loads(args.openclaw_payload)
            raw_text = data.get('content') or data.get('text') or args.openclaw_payload
        except json.JSONDecodeError:
            raw_text = args.openclaw_payload
        args.from_text = raw_text

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