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
    related_concepts: list[str] = field(default_factory=list)  # 可关联的知识概念


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
        """提取音频文件

        抖音平台：使用本地 douyin-api 服务获取下载链接
        其他平台：使用 yt-dlp 下载
        """
        # 抖音平台：使用本地 API
        if video_info.platform == "douyin":
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
            return False
        except subprocess.TimeoutExpired:
            logger.error("ffmpeg timeout (>120s)")
            return False
        except Exception as e:
            logger.error(f"Douyin audio extraction failed: {e}")
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
    """
    知识蒸馏引擎

    使用统一 LLM 客户端 (llm_client.py) 进行知识提取
    默认使用 kimi-k2.5 模型，自动降级到 qwen3-max-2026-01-23 → qwen3.5-plus → glm-5
    """

    SYSTEM_PROMPT = """你是一个专业的知识提取助手，擅长将视频内容转化为结构化的知识笔记。

**第一步：评估内容质量等级**

根据视频信息密度，判断内容等级：
- **A级（深度干货）**：技术教程、深度分析、方法论，信息密度高
- **B级（有价值介绍）**：产品介绍、工具分享、观点输出，信息密度中
- **C级（浅层内容）**：日常分享、简单演示，信息密度低
- **D级（信息稀薄）**：广告、重复内容、无实质信息

**第二步：根据等级输出 JSON**

```json
{
  "title": "主张式标题（提炼核心观点/价值，而非描述内容）",
  "core_insight": "核心观点（一句话陈述视频的核心论点或洞见）",
  "content_tier": "A/B/C/D",
  "key_points": [
    {"timestamp": "MM:SS", "insight": "洞察点（为什么重要/如何应用）"}
  ],
  "summary": "见下方摘要模板",
  "target_audience": "适合谁（如：前端开发者、产品经理、AI爱好者）",
  "use_cases": "适用场景（如：快速原型开发、自动化测试）",
  "tags": ["标签1", "标签2", "标签3"],
  "action_items": ["具体可执行的建议"],
  "references": ["提到的工具/项目名称"],
  "related_concepts": ["可关联的知识概念"]
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

**D级（信息稀薄）- 30字以内**：
结构：[一句话概括] + 建议
示例："产品广告视频，无实质内容，建议跳过。"

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

请提取知识点并输出 JSON 格式结果。"""

    def __init__(self, config: AppConfig):
        self.config = config
        self._init_client()

    def _init_client(self):
        """初始化统一 LLM 客户端"""
        # 导入统一客户端
        sys.path.insert(0, str(Path(__file__).parent.parent / "core"))
        from llm_client import llm_client

        self.llm_client = llm_client
        logger.info("Using unified LLM client with distill role (kimi-k2.5)")

    def distill(self, video_info: VideoInfo, transcript: TranscriptResult) -> DistilledKnowledge:
        """执行知识蒸馏（使用统一客户端自动降级）"""
        logger.info("Distilling knowledge with distill role (kimi-k2.5 + auto-fallback)")

        # 构建提示
        user_prompt = self.USER_PROMPT_TEMPLATE.format(
            platform=video_info.platform,
            duration=video_info.duration,
            author=video_info.author or "未知",
            transcript=transcript.to_text()[:8000]  # 限制长度
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
                related_concepts=data.get("related_concepts", [])
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

> **内容等级：{tier_display}** | 适合：{target_audience}

## 💡 核心观点
{core_insight}

## 🧠 关键洞察
{key_points}

## 📝 内容摘要
{summary}

{use_cases_section}

## 🔗 知识关联
{related_section}

## ✅ 行动建议
{action_items_section}

{references_section}

## 📹 原始信息
- 来源平台：{platform}
- 作者：{author}
- 原始链接：[点击查看]({source_url})

---
> 由知微系统生成，建议关联已有知识并添加个人洞察
'''

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir

    def write(self, video_info: VideoInfo, transcript: TranscriptResult,
              knowledge: DistilledKnowledge, noise_tags: list[str]) -> Path:
        """生成并写入 Markdown 文件"""
        # 准备内容
        date_str = datetime.now().strftime("%Y-%m-%d")
        tags_str = ", ".join(f'"{tag}"' for tag in knowledge.tags)

        # 内容等级显示
        tier_labels = {
            "A": "⭐⭐⭐ 深度干货",
            "B": "⭐⭐ 有价值",
            "C": "⭐ 浅层内容",
            "D": "⚠️ 信息稀薄"
        }
        tier_display = tier_labels.get(knowledge.content_tier, "未评估")

        # 格式化知识点（洞察式）
        key_points_str = "\n".join(
            f"- **[{kp['timestamp']}]** {kp['insight']}"
            for kp in knowledge.key_points
        )

        # 适用场景部分
        use_cases_section = ""
        if knowledge.use_cases:
            use_cases_section = f"## 🎯 适用场景\n{knowledge.use_cases}"

        # 知识关联部分
        related_section = "暂无关联"
        if knowledge.related_concepts:
            related_str = ", ".join(f"[[{c}]]" for c in knowledge.related_concepts)
            related_section = related_str

        # 行动建议部分（必显示）
        if knowledge.action_items:
            items_str = "\n".join(f"- [ ] {item}" for item in knowledge.action_items)
        else:
            items_str = "- [ ] 思考如何将此知识应用到实际场景"

        # 参考资源部分
        references_section = ""
        if knowledge.references:
            refs_str = "\n".join(f"- {ref}" for ref in knowledge.references)
            references_section = f"## 📚 参考资料\n{refs_str}"

        # 目标受众
        target_audience = knowledge.target_audience or "通用"

        # 生成 Markdown
        content = self.TEMPLATE.format(
            title=knowledge.title,
            source_url=video_info.original_url,
            date=date_str,
            tags=tags_str,
            content_tier=knowledge.content_tier,
            tier_display=tier_display,
            target_audience=target_audience,
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