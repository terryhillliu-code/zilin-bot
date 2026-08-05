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
try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args, **kwargs):
        """dotenv 不可用时的空操作兜底"""
        pass

# 导入统一的 API Key 获取函数
try:
    from zhiwei_common import get_api_key, get_asr_key, get_llm_key
except ImportError:
    from zhiwei_common import get_api_key, get_asr_key, get_llm_key

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

    # Cookie 过期（注意：不能用单纯 "cookie" 子串匹配——yt-dlp 加载 cookiefile
    # 时的正常日志也含 "cookie"，会把网络超时等真实错误误判为 cookie_expired。
    # 只匹配明确的过期/登录失效特征词。）
    if any(kw in combined for kw in ["fresh cookies", "cookies expired", "cookie expired", "登录过期", "请先登录", "login expired"]):
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
    _cached_video_data: dict = field(default=None, repr=False)
    _cached_video_url: str = field(default=None, repr=False)
    _cached_video_path: str = field(default=None, repr=False)


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
    # 作者观点与论据（2026-08-01）: [{claim, timestamp, evidences:[{content, type, timestamp}]}]
    # 忠实提取作者主张及其依据，不做评判；批判/关联深挖由用户按需触发（/read 批判层）
    author_claims: list[dict] = field(default_factory=list)
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

        # API 配置 - 使用分离的 key 管理器
        # ASR 专用 key（仅 DASHSCOPE_API_KEY 有效）
        self.dashscope_api_key = get_asr_key() or ""
        self.qwen_model = os.getenv("QWEN_MODEL", "qwen3.7-plus")
        self.asr_model = os.getenv("ASR_MODEL", "paraformer-realtime-v2")  # Recognition API 用 realtime 变体
        self.asr_policy = os.getenv("ASR_POLICY", "auto")
        self.local_asr_model = os.getenv("LOCAL_ASR_MODEL", "medium")

        # ⭐ v3.3 (2026-07-31): mimo-asr 云端语音识别(小米 MiMo)
        # 背景: 原云端 ASR 走 DashScope, 但 DASHSCOPE_API_KEY 已 401 失效;
        # mimo-asr 实测 4.7s/60s 音频、中英混合准确, 作为云端首选。
        # OpenAI 兼容协议, chat/completions + input_audio(base64)。
        self.mimo_api_key = os.getenv("MIMO_API_KEY", "")
        self.mimo_api_base = os.getenv("MIMO_API_BASE", "https://token-plan-cn.xiaomimimo.com").rstrip("/")
        self.mimo_asr_model = os.getenv("MIMO_ASR_MODEL", "mimo-v2.5-asr")

        # ⭐ v3.5 (2026-07-31): YouTube VM 转写前哨
        # VM 出海 12.7MB/s 但回程隧道仅 82KB/s——在 VM 侧下载+转写, 回传仅文本(几KB),
        # YouTube 处理提速 ~155倍。前哨挂了降级回本地隧道下载路径。
        self.yt_prefetch_url = os.getenv("ZHIWEI_YT_PREFETCH_URL", "http://127.0.0.1:18799")

        # 输出配置（视频笔记专属目录）
        base_output_dir = os.getenv("OUTPUT_DIR", "~/Documents/ZhiweiVault/70-79_个人笔记/75_视频笔记_Video-Distill")
        self.output_dir = Path(base_output_dir).expanduser()
        self.assets_dir = self.output_dir / "Assets"

        # ⭐ v3.1 (2026-07-31): 海外平台代理与 cookies
        # 本机直连 youtube.com DNS 不通; 靠阿里云日本 VM 的 SSH SOCKS5 隔离中转。
        # 平台感知: 仅海外站点走代理, 抖音/B站直连(走代理反而慢且可能触发风控)。
        self.overseas_proxy = os.getenv("ZHIWEI_VIDEO_PROXY", "socks5://127.0.0.1:18081")
        self.youtube_cookies = os.getenv(
            "YOUTUBE_COOKIES_FILE",
            str(Path.home() / "zhiwei-bot" / "secrets" / "youtube_cookies.txt"))

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


# 需要代理的海外平台(v3.1): 国内平台直连更快且避免风控
OVERSEAS_PLATFORMS = {"youtube", "tiktok", "twitter", "unknown"}


def needs_proxy(platform: str) -> bool:
    """平台是否需要走海外代理"""
    return platform in OVERSEAS_PLATFORMS


# ============================================================================
# 分享文本提取器
# ============================================================================

class ShareTextExtractor:
    """从各种格式的分享文本中提取视频 URL"""

    # 支持的 URL 模式（按优先级排序）
    URL_PATTERNS = [
        r'https?://v\.douyin\.com/[A-Za-z0-9_/-]+',           # 抖音短链
        r'https?://(?:www\.)?iesdouyin\.com/share/video/\d+', # ⭐ 2026-08-05: 抖音新版分享格式
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
        r'https?://youtu\.be/[A-Za-z0-9_-]+',                      # YouTube 短链
        r'https?://(?:www\.)?youtube\.com/shorts/[A-Za-z0-9_-]+',    # YouTube Shorts(2026-08-02 补: RSS 中 shorts 链接此前无法识别)
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
                # 注：口令垃圾清理已由上游 rstrip 中文标点覆盖
                # 再次清理尾部标点
                url = url.rstrip('，。！？、；：""''）】》./')
                # ⭐ 2026-08-05: iesdouyin 分享链接归一化为标准格式
                # （douyin-api :8680 只认 douyin.com/video/ID，不认 share/video）
                _m = re.match(r'https?://(?:www\.)?iesdouyin\.com/share/video/(\d+)', url)
                if _m:
                    url = f'https://www.douyin.com/video/{_m.group(1)}'
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
            # v3.0 (2026-07-31): 转写缓存 + 成本追踪列(自 video_cache.py 僵尸模块整合)
            existing_cols = {row[1] for row in conn.execute('PRAGMA table_info(processed)')}
            for col, ddl in [("transcript", "TEXT"), ("asr_engine", "TEXT"),
                             ("tokens_used", "INTEGER DEFAULT 0"), ("cost_usd", "REAL DEFAULT 0")]:
                if col not in existing_cols:
                    conn.execute(f'ALTER TABLE processed ADD COLUMN {col} {ddl}')

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

    def mark_processed(self, resolved_url: str, output_path: str, title: str = "", video_id: str = None,
                       transcript: str = "", asr_engine: str = "", tokens_used: int = 0, cost_usd: float = 0.0):
        """标记已处理(v3.0: 附带转写缓存与成本估算,供 --vision 重跑免二次 ASR)"""
        import sqlite3
        vid = video_id or self.extract_video_id(resolved_url)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''INSERT OR REPLACE INTO processed
                (video_id, resolved_url, title, output_path, transcript, asr_engine, tokens_used, cost_usd)
                VALUES (?,?,?,?,?,?,?,?)''',
                (vid or None, resolved_url, title, str(output_path),
                 transcript, asr_engine, tokens_used, cost_usd))
        logger.info(f"Marked as processed: {vid or resolved_url}")

    def get_cached_transcript(self, resolved_url: str, video_id: str = None) -> Optional[tuple]:
        """读取缓存转写(video_id 优先)。返回 (transcript, asr_engine) 或 None"""
        import sqlite3
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = None
            if video_id:
                row = conn.execute('SELECT transcript, asr_engine FROM processed WHERE video_id=?',
                                   (video_id,)).fetchone()
            if (row is None or not row["transcript"]) and resolved_url:
                row = conn.execute('SELECT transcript, asr_engine FROM processed WHERE resolved_url=?',
                                   (resolved_url,)).fetchone()
            if row and row["transcript"]:
                return row["transcript"], row["asr_engine"] or "cache"
        return None

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

        # 优先使用 play_addr 的 CDN 签名链接（无需 Cookie），
        # download_addr 的 aweme/v1/play 链接需要 Cookie 会 403
        for key in ["play_addr", "download_addr"]:
            addr = v.get(key, {})
            url_list = addr.get("url_list", [])
            # 优先选 CDN 签名链接（douyinvod.com），跳过需要 Cookie 的 play API
            for u in url_list:
                if "douyinvod.com" in u:
                    return u
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

    def __init__(self, cookies_browser: Optional[str] = None, cookies_file: Optional[str] = None,
                 overseas_proxy: Optional[str] = None, youtube_cookies: Optional[str] = None):
        self.yt_dlp_path = self._find_yt_dlp()
        self.cookies_browser = cookies_browser
        self.cookies_file = cookies_file
        # v3.1: 海外平台代理 + YouTube 专属 cookies(过 bot 检测)
        self.overseas_proxy = overseas_proxy
        self.youtube_cookies = youtube_cookies

    def _net_opts(self, platform: str) -> dict:
        """构造平台感知的 yt-dlp 网络选项(代理 + cookies)

        v3.1: 海外平台走 SOCKS5(日本 VM 隔离); YouTube 优先用专属 cookies 文件。
        v3.2: YouTube 音视频流解锁三件套——
          1. POT: VM 上 bgutil provider(systemd bgutil-pot, 经隧道 14416→4416)生成
             Proof-of-Origin Token, 绕过"Sign in to confirm you're not a bot"风控。
             POT 必须与下载出口同 IP 生成, 故跑在 VM 侧(VM 本地 microsocks:18081
             镜像本机隧道端口, 使 yt-dlp 透传的 --proxy 参数在两端都可解析)。
          2. EJS: 允许从 GitHub 拉 n-challenge 求解脚本(缺它拿不到可用 format URL)。
          3. cookies: 登录态过 bot 检测。
        """
        opts = {}
        if needs_proxy(platform) and self.overseas_proxy:
            opts["proxy"] = self.overseas_proxy
            # 隧道链路抖动会导致下载中途断连(实测: N bytes read, M more expected),
            # 加重试+断点续传兼容
            opts.update({"retries": 10, "fragment_retries": 10,
                         "socket_timeout": 30, "continuedl": True})
        if platform == "youtube":
            pot_base = os.getenv("ZHIWEI_POT_BASE_URL", "http://127.0.0.1:14416")
            opts["extractor_args"] = {"youtubepot-bgutilhttp": {"base_url": [pot_base]}}
            opts["remote_components"] = ["ejs:github"]
        # cookies 优先级(2026-08-02 修正): 显式浏览器 > 显式文件 > YouTube 专属文件。
        # 原顺序把 youtube_cookies.txt 默认文件排在浏览器之前, 该文件被 YouTube
        # 服务端吊销后, --cookies-from-browser 永远轮不到生效, bot 检测全灭。
        if self.cookies_browser:
            opts["cookiesfrombrowser"] = (self.cookies_browser,)
        elif self.cookies_file and Path(self.cookies_file).exists():
            opts["cookiefile"] = self.cookies_file
        elif platform == "youtube" and self.youtube_cookies and Path(self.youtube_cookies).exists():
            opts["cookiefile"] = self.youtube_cookies
        return opts

    def _requests_proxies(self, platform: str) -> Optional[dict]:
        """requests 库的代理字典(字幕文件下载等直调场景)

        ⭐ socks5 → socks5h: 前者在本地解析 DNS(本机解不了 youtube.com 会直接失败),
        后者把 DNS 解析也交给代理端(等价 curl --socks5-hostname)。
        """
        if needs_proxy(platform) and self.overseas_proxy:
            proxy = self.overseas_proxy
            if proxy.startswith("socks5://"):
                proxy = proxy.replace("socks5://", "socks5h://", 1)
            return {"http": proxy, "https": proxy}
        return None

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

    def _get_douyin_video_info(self, video_info: VideoInfo) -> tuple:
        """获取抖音视频数据和下载 URL，带缓存"""
        if video_info._cached_video_data is not None:
            return video_info._cached_video_data, video_info._cached_video_url
        client = DouyinAPIClient()
        video_data, video_url = client.get_video_info(video_info.original_url)
        video_info._cached_video_data = video_data
        video_info._cached_video_url = video_url
        if not video_info.title:
            video_info.title = video_data.get("desc", "")[:100]
        if not video_info.author:
            author_info = video_data.get("author", {})
            video_info.author = author_info.get("nickname", "")
        return video_data, video_url

    def extract_subtitles(self, video_info: VideoInfo) -> Optional[TranscriptResult]:
        """提取平台字幕（如果有）

        v2.1: 抖音平台使用 DouyinAPIClient 获取文案作为字幕
        v3.1: B站走官方 API(yt-dlp 网页解析必 412); YouTube 走代理+cookies
              且容忍 format 不可用(PO Token 风控下字幕仍可取)
        """
        # ⭐ 抖音平台：使用本地 API 获取文案作为字幕
        if video_info.platform == "douyin":
            return self._extract_douyin_subtitle(video_info)

        # ⭐ v3.1 B站：yt-dlp 网页解析被风控拦截(HTTP 412)，改走官方 player API
        if video_info.platform == "bilibili":
            return self._extract_bilibili_subtitle(video_info)

        # 其他平台：使用 yt-dlp
        import yt_dlp

        logger.info(f"Extracting subtitles from {video_info.resolved_url}")

        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["zh-Hans", "zh", "zh-CN", "en"],
            # ⭐ v3.1 关键: YouTube PO Token 风控下只剩 storyboard format,
            # 不容忍会让 extract_info 直接抛异常，连字幕一起丢掉。
            "ignore_no_formats_error": True,
        }
        ydl_opts.update(self._net_opts(video_info.platform))
        if ydl_opts.get("proxy"):
            logger.info(f"Using overseas proxy: {ydl_opts['proxy']}")

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(video_info.resolved_url, download=False)
                if not info:
                    logger.info("No video info returned")
                    return None

                # 更新视频信息
                video_info.title = info.get("title", "")
                video_info.author = info.get("uploader", "")
                video_info.duration = info.get("duration", 0)
                video_info.description = info.get("description", "")

                # ⭐ v3.1 修复: 原实现 `subtitles or automatic_captions` 短路——
                # 人工字幕非空但无中英文时不会回退到自动字幕。现合并两者,
                # 人工字幕优先(质量更高)。
                manual = info.get("subtitles") or {}
                auto = info.get("automatic_captions") or {}
                if not manual and not auto:
                    logger.info("No subtitles found")
                    return None

                pref_langs = ["zh-Hans", "zh", "zh-CN", "zh-Hant", "en", "en-orig"]
                for pool, pool_name in ((manual, "manual"), (auto, "auto")):
                    for lang in pref_langs:
                        if lang in pool and pool[lang]:
                            logger.info(f"Using {pool_name} subtitles: {lang}")
                            result = self._download_and_parse_subtitles(
                                pool[lang][0]["url"], platform=video_info.platform)
                            if result and result.full_text:
                                result.source = f"platform_subtitle_{pool_name}_{lang}"
                                return result

                logger.info(f"No usable subtitles (manual={list(manual)[:3]}, auto={list(auto)[:3]})")
                return None

        except Exception as e:
            logger.error(f"Error extracting subtitles: {e}")
            return None

    def _extract_bilibili_subtitle(self, video_info: VideoInfo) -> Optional[TranscriptResult]:
        """通过 B站官方 API 提取字幕(v3.1)

        yt-dlp 的 bilibili extractor 与当前风控不兼容，网页解析恒返 HTTP 412
        (带完整 buvid3/buvid4 cookies 亦无法绕过)，但 api.bilibili.com 直接可用。
        同时回填 title/author/duration 供后续蒸馏使用。
        """
        try:
            bvid_match = re.search(r'BV(\w+)', video_info.resolved_url)
            if not bvid_match:
                logger.warning(f"Cannot extract bvid: {video_info.resolved_url}")
                return None
            bvid = f"BV{bvid_match.group(1)}"

            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://www.bilibili.com",
            }
            cookies = self._load_cookie_header()
            if cookies:
                headers["Cookie"] = cookies

            # 1. view API: 拿 cid 与元信息
            view = requests.get(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
                               headers=headers, timeout=15).json()
            if view.get("code") != 0:
                logger.warning(f"B站 view API: {view.get('code')} {view.get('message')}")
                return None
            data = view["data"]
            cid = data["cid"]
            video_info.title = video_info.title or data.get("title", "")
            video_info.author = video_info.author or (data.get("owner") or {}).get("name", "")
            video_info.duration = video_info.duration or data.get("duration", 0)

            # 2. player/v2 API: 拿字幕列表(需登录 cookies 才能拿到 AI 字幕)
            player = requests.get(
                f"https://api.bilibili.com/x/player/v2?bvid={bvid}&cid={cid}",
                headers=headers, timeout=15).json()
            subs = ((player.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
            if not subs:
                logger.info("B站无可用字幕，降级到 ASR")
                return None

            # 优先中文字幕
            chosen = next((s for s in subs if str(s.get("lan", "")).startswith("zh")), subs[0])
            sub_url = chosen.get("subtitle_url") or ""
            if sub_url.startswith("//"):
                sub_url = "https:" + sub_url
            if not sub_url:
                return None

            # 3. 下载 B站 JSON 字幕并转 TranscriptResult
            body = requests.get(sub_url, headers=headers, timeout=20).json().get("body") or []
            segments = [TranscriptSegment(start=float(it.get("from", 0)),
                                          end=float(it.get("to", 0)),
                                          text=str(it.get("content", "")).strip())
                        for it in body if it.get("content")]
            if not segments:
                return None
            full_text = " ".join(s.text for s in segments)
            logger.info(f"B站字幕提取成功: {chosen.get('lan')} / {len(full_text)} 字符")
            return TranscriptResult(segments=segments, full_text=full_text,
                                    source=f"bilibili_api_subtitle_{chosen.get('lan', '')}",
                                    language="zh", confidence=0.95)
        except Exception as e:
            logger.warning(f"B站字幕 API 失败(降级 ASR): {e}")
            return None

    def _load_cookie_header(self) -> str:
        """从 Netscape cookies 文件拼 Cookie 请求头(B站 API 用)"""
        path = self.cookies_file
        if not path or not Path(path).exists():
            return ""
        try:
            pairs = []
            for line in Path(path).read_text(errors="ignore").splitlines():
                if line.startswith("#") or not line.strip():
                    continue
                parts = line.split("\t")
                if len(parts) >= 7 and "bilibili" in parts[0]:
                    pairs.append(f"{parts[5]}={parts[6]}")
            return "; ".join(pairs)
        except OSError:
            return ""

    def _extract_douyin_subtitle(self, video_info: VideoInfo) -> Optional[TranscriptResult]:
        """从抖音 API 获取视频文案作为字幕

        Args:
            video_info: 视频信息

        Returns:
            TranscriptResult（文案作为字幕），失败返回 None
        """
        try:
            logger.info(f"Fetching douyin video info for subtitle: {video_info.original_url}")
            if video_info._cached_video_data is not None:
                video_data = video_info._cached_video_data
            else:
                video_data, _ = self._get_douyin_video_info(video_info)

            # 抖音视频的 desc 字段通常是字幕/文案内容
            desc = video_data.get("desc", "")
            if not desc:
                logger.info("No description/subtitle from Douyin API")
                return None

            # 更新视频信息
            video_info.title = desc[:100] if len(desc) > 100 else desc

            author_info = video_data.get("author", {})
            video_info.author = author_info.get("nickname", "")

            # 构造 TranscriptResult（抖音文案作为完整文本）
            segment = TranscriptSegment(
                start=0.0,
                end=float(video_data.get("video", {}).get("duration", 0)),
                text=desc
            )

            logger.info(f"Douyin subtitle extracted: {len(desc)} characters")

            return TranscriptResult(
                segments=[segment],
                full_text=desc,
                source="douyin_desc",
                language="zh",
                confidence=1.0
            )

        except Exception as e:
            logger.error(f"Douyin subtitle extraction failed: {e}")
            return None

    def _download_and_parse_subtitles(self, url: str, platform: str = "unknown") -> TranscriptResult:
        """下载并解析字幕文件

        v3.1: 海外平台字幕 URL(如 YouTube timedtext)必靠代理，否则 DNS 不通。
        """
        try:
            response = requests.get(url, timeout=30, proxies=self._requests_proxies(platform))
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
        """解析字幕内容（SRT/VTT/json3 格式）

        ⭐ v3.1: YouTube 的 timedtext 接口默认返 json3(URL 带 fmt=json3),
        原实现只认 SRT/VTT 会默默解出 0 段，导致字幕明明拿到却被当成“无字幕”。
        """
        stripped = content.lstrip()
        if stripped.startswith("{"):
            json3 = self._parse_json3_subtitle(stripped)
            if json3:
                return json3

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
    def _parse_json3_subtitle(content: str) -> list[TranscriptSegment]:
        """解析 YouTube json3 字幕

        结构: {"events": [{"tStartMs":233, "dDurationMs":2567, "segs":[{"utf8":"文本"}]}]}
        无 segs 的 event 为空行/换行标记，跳过。
        """
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return []
        segments = []
        for ev in data.get("events") or []:
            segs = ev.get("segs")
            if not segs:
                continue
            text = "".join(s.get("utf8", "") for s in segs).strip()
            if not text or text == "\n":
                continue
            start = float(ev.get("tStartMs", 0)) / 1000.0
            end = start + float(ev.get("dDurationMs", 0)) / 1000.0
            segments.append(TranscriptSegment(start=start, end=end, text=text))
        if segments:
            logger.info(f"Parsed json3 subtitle: {len(segments)} segments")
        return segments

    @staticmethod
    def _srt_time_to_seconds(h: str, m: str, s: str, ms: str) -> float:
        """将 SRT 时间转换为秒"""
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000

    def download_video(self, video_info: VideoInfo, output_path: Path, max_height: int = None) -> bool:
        """下载视频文件（用于图片视频检测、--vision 抽帧等场景）

        Args:
            video_info: 视频信息
            output_path: 视频输出路径
            max_height: 限制最大分辨率(如 480,用于 vision 抽帧省流量); None 为默认画质

        Returns:
            是否成功
        """
        # 抖音平台：使用本地 API
        if video_info.platform == "douyin":
            return self._download_douyin_video(video_info, output_path)

        # 其他平台：使用 yt-dlp
        import yt_dlp

        logger.info(f"Downloading video to {output_path}")

        if max_height:
            fmt = f"best[height<={max_height}][ext=mp4]/best[height<={max_height}]/best"
        else:
            fmt = "best[ext=mp4]/best"
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "format": fmt,
            # 2026-08-02 修复: outtmpl 必须显式带 %(ext)s。否则 yt-dlp 按字面
            # 输出无扩展名文件(实测生成 "video" 而非 "video.mp4"), 后续
            # output_path.exists() 检查永远失败, 视觉分析被静默跳过。
            "outtmpl": str(output_path.with_suffix("")) + ".%(ext)s",
        }
        # v3.1: 平台感知代理 + cookies
        ydl_opts.update(self._net_opts(video_info.platform))

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
            logger.info(f"Fetching douyin video info for download: {video_info.original_url}")
            video_data, video_url = self._get_douyin_video_info(video_info)
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
        B站：使用 B站 API + Referer 头下载音频流
        其他平台：使用 yt-dlp 下载

        Raises:
            ValueError: 抖音 API 调用失败时抛出，包含详细错误信息
        """
        # 抖音平台：使用本地 API
        if video_info.platform == "douyin":
            return self._extract_douyin_audio(video_info, output_path)

        # B站：使用 API 直接下载音频流（绕过 yt-dlp 412 错误）
        if video_info.platform == "bilibili":
            return self._extract_bilibili_audio(video_info, output_path)

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
        # v3.1: 平台感知代理 + cookies
        ydl_opts.update(self._net_opts(video_info.platform))

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

    def _extract_bilibili_audio(self, video_info: VideoInfo, output_path: Path) -> bool:
        """通过 B站 API 直接下载音频流（绕过 yt-dlp 412 错误）

        B站反爬机制导致 yt-dlp 无法直接下载，需要：
        1. 通过 API 获取视频 cid
        2. 通过 playurl API 获取音频流地址
        3. 带 Referer 头下载音频

        Args:
            video_info: 视频信息（从 resolved_url 提取 bvid）
            output_path: 音频输出路径

        Returns:
            是否成功
        """
        m4s_path = None

        try:
            # 从 resolved_url 提取 bvid
            bvid_match = re.search(r'BV(\w+)', video_info.resolved_url)
            if bvid_match:
                bvid = f"BV{bvid_match.group(1)}"
            else:
                logger.error(f"Cannot extract bvid from URL: {video_info.resolved_url}")
                return False

            logger.info(f"Fetching B站 video info: {bvid}")

            # B站 API 需要带 User-Agent 和 Referer 头
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://www.bilibili.com"
            }

            # 1. 获取 cid
            info_url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
            info_resp = requests.get(info_url, headers=headers, timeout=10)

            logger.debug(f"B站 API response status: {info_resp.status_code}")

            if info_resp.status_code != 200:
                logger.error(f"B站 API HTTP error: {info_resp.status_code}")
                return False

            try:
                info_data = info_resp.json()
            except Exception as json_err:
                logger.error(f"B站 API JSON parse error: {json_err}, response: {info_resp.text[:200]}")
                return False

            if info_data.get("code") != 0:
                logger.error(f"B站 API error: {info_data.get('message')}")
                return False

            video_data = info_data.get("data", {})
            cid = video_data.get("cid")
            title = video_data.get("title", "")

            if not video_info.title:
                video_info.title = title

            logger.info(f"B站 cid: {cid}, title: {title}")

            # 2. 获取音频流地址
            playurl = f"https://api.bilibili.com/x/player/playurl?bvid={bvid}&cid={cid}&qn=16&fnver=0&fnval=16&fourk=0"
            play_resp = requests.get(playurl, headers=headers, timeout=10)
            play_data = play_resp.json()

            if play_data.get("code") != 0:
                logger.error(f"B站 playurl API error: {play_data.get('message')}")
                return False

            dash = play_data.get("data", {}).get("dash", {})
            audio_streams = dash.get("audio", [])

            if not audio_streams:
                logger.error("No audio streams found")
                return False

            # 选择最高质量的音频流
            audio_streams.sort(key=lambda x: x.get("id", 0), reverse=True)
            audio_url = audio_streams[0].get("baseUrl") or audio_streams[0].get("base_url")

            if not audio_url:
                logger.error("No audio URL found")
                return False

            logger.info(f"B站 audio URL: {audio_url[:80]}...")

            # 3. 下载音频流（需要 Referer 头）
            m4s_path = output_path.with_suffix(".m4s")
            mp3_path = output_path.with_suffix(".mp3")

            logger.info(f"Downloading B站 audio to {m4s_path}")
            audio_resp = requests.get(audio_url, headers=headers, stream=True, timeout=300)

            with open(m4s_path, "wb") as f:
                for chunk in audio_resp.iter_content(chunk_size=8192):
                    f.write(chunk)

            logger.info(f"Audio downloaded: {m4s_path.stat().st_size / 1024 / 1024:.1f} MB")

            # 4. 转换为 MP3
            result = subprocess.run(
                ["ffmpeg", "-y", "-i", str(m4s_path), "-vn", "-acodec", "libmp3lame", "-q:a", "2", str(mp3_path)],
                capture_output=True, text=True, timeout=120
            )

            if result.returncode != 0:
                logger.error(f"FFmpeg error: {result.stderr}")
                return False

            logger.info(f"B站 audio extracted: {mp3_path}")
            return True

        except Exception as e:
            logger.error(f"B站 audio extraction error: {e}")
            return False

        finally:
            # 清理临时文件（无论成功还是失败）
            if m4s_path and m4s_path.exists():
                m4s_path.unlink(missing_ok=True)

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
            mp3_path = output_path.with_suffix(".mp3")
            video_tmp = output_path.with_suffix(".mp4")
            reused_cached = False

            # 复用 _try_image_video 已下载的视频文件
            if video_info._cached_video_path and os.path.exists(video_info._cached_video_path):
                import shutil
                shutil.move(video_info._cached_video_path, str(video_tmp))
                video_info._cached_video_path = None
                reused_cached = True
                logger.info(f"Reusing cached video file: {video_tmp} ({video_tmp.stat().st_size} bytes)")
            else:
                logger.info(f"Fetching douyin video info via local API: {video_info.original_url}")
                video_data, video_url = self._get_douyin_video_info(video_info)
                logger.info(f"Douyin video URL obtained: {video_url[:80]}...")

                headers = {
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Referer": "https://www.douyin.com/",
                    "Accept": "*/*",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                }

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
                return True
            else:
                stderr = result.stderr.decode('utf-8', errors='replace')
                logger.error(f"ffmpeg error (returncode={result.returncode}): {stderr[:500]}")
                return False

        except ValueError as e:
            logger.error(f"Douyin API error: {e}")
            raise
        except subprocess.TimeoutExpired:
            logger.error("ffmpeg timeout (>120s)")
            raise TimeoutError("ffmpeg timeout while extracting audio")
        except Exception as e:
            logger.error(f"Douyin audio extraction failed: {e}")
            raise
        finally:
            if video_tmp.exists():
                try:
                    video_tmp.unlink()
                except OSError:
                    pass


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

    def __init__(self, api_key: str, model: str = "paraformer-realtime-v2"):  # Recognition API 专用模型名
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
# Mimo ASR 云端语音识别器 (v3.3, 小米 MiMo)
# ============================================================================

class MimoASRTranscriber(BaseTranscriber):
    """小米 MiMo 云端 ASR (mimo-v2.5-asr, OpenAI 兼容协议)

    替代已 401 失效的 DashScope 云端 ASR。实测 4.7s/60s 音频、中英混合准。
    调用: POST {base}/v1/chat/completions, messages 内 input_audio(base64 wav)。
    ⚠️ ASR 请求不能带 text part(网关自注入 prompt)。
    长音频按 ~5min 分片串行转写后拼接, 单片 base64 不至过大。
    """

    CHUNK_SECONDS = 300         # 每片 5 分钟(16k 单声道 wav 约 9.6MB, base64 ~13MB)
    MAX_CHUNKS = 40             # 上限 200 分钟, 覆盖超长播客(Lex 3小时级); 超长交给 MLX

    def __init__(self, api_key: str, api_base: str, model: str = "mimo-v2.5-asr"):
        self.api_key = api_key
        self.api_base = (api_base or "https://token-plan-cn.xiaomimimo.com").rstrip("/")
        self.model = model
        self._available = bool(api_key)

    def is_available(self) -> bool:
        return self._available

    def _audio_duration(self, audio_path: Path) -> float:
        try:
            r = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(audio_path)],
                capture_output=True, text=True, timeout=30)
            return float(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else 0.0
        except Exception:
            return 0.0

    def _transcribe_clip(self, wav_path: Path) -> str:
        """转写单个 16k 单声道 wav 片段"""
        import base64
        b64 = base64.b64encode(wav_path.read_bytes()).decode()
        resp = requests.post(
            f"{self.api_base}/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=120,
            json={"model": self.model, "messages": [{"role": "user", "content": [
                {"type": "input_audio", "input_audio": {"data": b64, "format": "wav"}}]}]})
        if resp.status_code != 200:
            raise RuntimeError(f"mimo-asr HTTP {resp.status_code}: {resp.text[:200]}")
        return (resp.json()["choices"][0]["message"]["content"] or "").strip()

    def transcribe(self, audio_path: Path) -> TranscriptResult:
        if not self._available:
            raise RuntimeError("MIMO_API_KEY not configured")
        logger.info(f"Transcribing with mimo-asr {self.model}: {audio_path}")
        try:
            duration = self._audio_duration(audio_path)
            with tempfile.TemporaryDirectory(prefix="mimo_asr_") as td:
                tdp = Path(td)
                if duration <= self.CHUNK_SECONDS:
                    # 短音频: 整段转 16k 单声道 wav 一次转写
                    wav = tdp / "clip.wav"
                    subprocess.run(["ffmpeg", "-y", "-i", str(audio_path),
                                    "-ar", "16000", "-ac", "1", str(wav)],
                                   capture_output=True, timeout=120)
                    if not wav.exists():
                        return TranscriptResult()
                    text = self._transcribe_clip(wav)
                else:
                    # 长音频: 按 CHUNK_SECONDS 分片串行转写后拼接
                    n = min(self.MAX_CHUNKS, int(duration // self.CHUNK_SECONDS) + 1)
                    logger.info(f"mimo-asr 长音频分 {n} 片({duration:.0f}s)")
                    parts = []
                    for i in range(n):
                        seg = tdp / f"seg_{i}.wav"
                        subprocess.run(["ffmpeg", "-y", "-ss", str(i * self.CHUNK_SECONDS),
                                        "-t", str(self.CHUNK_SECONDS), "-i", str(audio_path),
                                        "-ar", "16000", "-ac", "1", str(seg)],
                                       capture_output=True, timeout=120)
                        if seg.exists() and seg.stat().st_size > 1024:
                            try:
                                parts.append(self._transcribe_clip(seg))
                            except Exception as e:
                                logger.warning(f"mimo-asr 片 {i+1}/{n} 失败: {e}")
                    text = " ".join(p for p in parts if p)
                if not text:
                    return TranscriptResult()
                return TranscriptResult(segments=[], full_text=text,
                                        source="mimo_asr", language="zh", confidence=0.9)
        except Exception as e:
            logger.error(f"mimo-asr transcription error: {e}")
            return TranscriptResult(error_details=str(e))


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
# 视觉分析器 (v3.0 --vision 模式, 2026-07-31)
# ============================================================================

class VisionAnalyzer:
    """按需视觉分析: 场景切换抽帧 + VLM 图表/关键画面结构化提取

    与 ImageVideoProcessor 的区别: 后者面向图片轮播类视频(替代 ASR),
    本类面向普通视频的补充分析——提取图表/数据/演示画面作为蒸馏辅助信息。
    默认关闭,仅 --vision 标志触发(看完视频后手动二次提交)。
    """

    SCENE_THRESHOLD = 0.3   # 场景切换阈值(高于图片视频检测的0.15,只取明显切换)
    MAX_FRAMES = 10         # VLM 成本上限
    MIN_FRAMES = 3          # 不足时降级均匀采样
    # 2026-08-02 清晰度修复: 480p 抽帧图表文字看不清, 升 720p(流量约 2 倍但
    # 仍是短视频流, 实测可接受); 时机修复: 切换帧常是转场模糊帧, 后移 1.5s
    # 取场景稳定画面(见 _extract_scene_frames 两阶段实现)
    DOWNLOAD_MAX_HEIGHT = 720
    FRAME_DELAY_SEC = 1.5   # 场景切换点后移秒数, 避开转场模糊帧

    VISION_PROMPT = """分析这一视频关键帧,重点关注图表、数据、代码、演示界面等承载关键信息的画面。
必须按以下 JSON 格式输出(不要额外解释文字):
{
  "type": "chart|table|diagram|code|slide|scene",
  "title": "画面标题(如有)",
  "key_insights": ["关键发现1", "关键发现2"],
  "data_summary": "图表/表格中的具体数值、对比、趋势(尽量完整提取);非数据画面填空字符串",
  "description": "50-150字描述"
}
若画面无实质信息(纯人像/转场/黑屏),type 填 "scene" 且 key_insights 留空。"""

    def __init__(self, config: AppConfig):
        self.config = config
        # 复用 ImageVideoProcessor 的 VLM 引擎加载逻辑(百炼 qwen-vl)
        self._img_processor = ImageVideoProcessor(config)

    def analyze(self, video_info: VideoInfo, media_extractor: "MediaExtractor") -> list[ImageFrame]:
        """下载视频(≤480p) → 场景抽帧 → VLM 描述。返回带描述的帧列表(持久临时目录)。

        帧文件位于 mkdtemp 目录,由调用方(MarkdownWriter)负责拷入 Assets 并清理。
        失败返回空列表(不阻断主流程)。
        """
        frames_dir = Path(tempfile.mkdtemp(prefix="vision_frames_"))
        video_path = None
        cleanup_video = False
        try:
            # 1. 获取视频文件(优先复用图片视频检测阶段缓存的下载)
            cached = getattr(video_info, "_cached_video_path", None)
            if cached and Path(cached).exists():
                video_path = Path(cached)
                logger.info(f"[vision] 复用已下载视频: {video_path}")
            else:
                video_path = frames_dir / "video.mp4"
                cleanup_video = True
                logger.info(f"[vision] 下载视频流(≤{self.DOWNLOAD_MAX_HEIGHT}p)用于抽帧...")
                if not media_extractor.download_video(video_info, video_path,
                                                      max_height=self.DOWNLOAD_MAX_HEIGHT):
                    logger.error("[vision] 视频下载失败,跳过视觉分析")
                    return []

            # 2. 场景切换抽帧
            frames = self._extract_scene_frames(video_path, frames_dir)
            if len(frames) < self.MIN_FRAMES:
                logger.info(f"[vision] 场景帧不足({len(frames)}),降级均匀采样")
                frames = self._img_processor.extract_key_frames(video_path, frames_dir)
                frames = frames[:self.MAX_FRAMES]
            if not frames:
                logger.error("[vision] 抽帧失败")
                return []

            # 3. VLM 逐帧结构化描述(复用 ImageVideoProcessor 的 VLM 引擎)
            frames = self._img_processor.describe_frames(frames, prompt=self.VISION_PROMPT)
            valid = [f for f in frames if f.description and not f.description.startswith("[")]
            logger.info(f"[vision] 视觉分析完成: {len(valid)}/{len(frames)} 帧有效")
            return frames
        except Exception as e:
            logger.error(f"[vision] 视觉分析异常(不阻断主流程): {e}")
            return []
        finally:
            # 只清视频文件,帧文件保留待 writer 拷走
            if cleanup_video and video_path and video_path.exists():
                try:
                    video_path.unlink()
                except OSError:
                    pass

    def _extract_scene_frames(self, video_path: Path, output_dir: Path) -> list[ImageFrame]:
        """两阶段场景抽帧(2026-08-02 时机修复):
        阶段1 ffmpeg 场景检测只取切换点时间戳(showinfo, 不产图);
        阶段2 在每个切换点 +FRAME_DELAY_SEC 处精确取帧——
        直接抓切换帧往往是转场模糊/信息未出全的画面, 后移取场景稳定帧。"""
        frames: list[ImageFrame] = []
        detect_cmd = [
            "ffmpeg", "-y", "-i", str(video_path),
            "-vf", f"select='gt(scene,{self.SCENE_THRESHOLD})',showinfo",
            "-vsync", "vfr", "-frames:v", str(self.MAX_FRAMES),
            "-f", "null", "-",
        ]
        try:
            result = subprocess.run(detect_cmd, capture_output=True, text=True, timeout=300)
            timestamps = [float(m) for m in re.findall(r"pts_time:([\d.]+)", result.stderr or "")]
            # 视频时长(避免后移越界)
            dur_m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", result.stderr or "")
            duration = (int(dur_m.group(1)) * 3600 + int(dur_m.group(2)) * 60
                        + float(dur_m.group(3))) if dur_m else float("inf")

            for i, ts in enumerate(timestamps[:self.MAX_FRAMES]):
                grab = ts + self.FRAME_DELAY_SEC
                if grab >= duration:
                    grab = ts  # 结尾附近后移越界则退回切换帧本身
                fp = output_dir / f"scene_{i:03d}.jpg"
                grab_cmd = ["ffmpeg", "-y", "-ss", f"{grab:.2f}", "-i", str(video_path),
                            "-frames:v", "1", "-q:v", "2", str(fp)]
                try:
                    r2 = subprocess.run(grab_cmd, capture_output=True, timeout=60)
                    if r2.returncode == 0 and fp.exists() and fp.stat().st_size > 0:
                        frames.append(ImageFrame(index=i, timestamp=grab, path=str(fp)))
                except subprocess.TimeoutExpired:
                    logger.warning(f"[vision] 取帧超时(ts={grab:.1f})")
            logger.info(f"[vision] 场景抽帧: {len(frames)} 帧(延迟{self.FRAME_DELAY_SEC}s取稳定帧)")
        except subprocess.TimeoutExpired:
            logger.warning("[vision] 场景检测超时")
        except Exception as e:
            logger.warning(f"[vision] 场景抽帧失败: {e}")
        return frames

    @staticmethod
    def build_visual_context(frames: list[ImageFrame]) -> str:
        """将帧描述合成为蒸馏 prompt 的「视觉信息」段落(过滤无信息帧)"""
        parts = []
        for f in frames:
            if not f.description or f.description.startswith("["):
                continue
            info = VisionAnalyzer.parse_frame_json(f.description)
            if info.get("type") == "scene" and not info.get("key_insights"):
                continue  # 无实质信息画面
            ts = f"{int(f.timestamp // 60):02d}:{int(f.timestamp % 60):02d}"
            seg = [f"[{ts}] ({info.get('type', 'scene')}) {info.get('title', '')}".strip()]
            if info.get("data_summary"):
                seg.append(f"  数据: {info['data_summary']}")
            if info.get("key_insights"):
                seg.append("  要点: " + "; ".join(info["key_insights"]))
            if info.get("description"):
                seg.append(f"  描述: {info['description']}")
            parts.append("\n".join(seg))
        if not parts:
            return ""
        return "**视觉信息(关键帧 VLM 提取,含图表/数据,请与转录交叉分析):**\n" + "\n\n".join(parts)

    @staticmethod
    def parse_frame_json(description: str) -> dict:
        """解析单帧 VLM JSON 输出;失败时降级为纯文本描述"""
        try:
            m = re.search(r"\{[\s\S]*\}", description)
            if m:
                return json.loads(m.group())
        except (json.JSONDecodeError, TypeError):
            pass
        return {"type": "scene", "title": "", "key_insights": [],
                "data_summary": "", "description": description.strip()}


# ============================================================================
# 转录提供者（路由编排）
# ============================================================================

class TranscriptProvider:
    """转录服务路由"""

    def __init__(self, config: AppConfig, cookies_browser: Optional[str] = None, cookies_file: Optional[str] = None):
        self.config = config
        self.cookies_browser = cookies_browser
        self.cookies_file = cookies_file
        self.media_extractor = MediaExtractor(
            cookies_browser, cookies_file,
            overseas_proxy=getattr(config, "overseas_proxy", None),
            youtube_cookies=getattr(config, "youtube_cookies", None),
        )
        self.dashscope_transcriber = DashScopeASRTranscriber(
            config.dashscope_api_key,
            config.asr_model
        )
        # v3.3: mimo-asr 云端首选(DashScope 已 401 失效)
        self.mimo_transcriber = MimoASRTranscriber(
            getattr(config, "mimo_api_key", ""),
            getattr(config, "mimo_api_base", ""),
            getattr(config, "mimo_asr_model", "mimo-v2.5-asr"),
        )
        self.local_transcriber = LocalMLXWhisperTranscriber(config.local_asr_model)
        self.image_video_processor = ImageVideoProcessor(config)

    def get_transcript(self, video_info: VideoInfo, save_audio_path: Optional[Path] = None) -> TranscriptResult:
        """获取转录文本，按策略选择方法

        v2.2: 抖音平台跳过字幕提取，因为视频描述（hashtags）不是真正的语音内容
        """
        policy = self.config.asr_policy

        # v3.5: YouTube 全部交给 VM 转写前哨(字幕+ASR 都在 VM 侧完成, 回传仅文本)
        # 必须前置于本地 extract_subtitles——否则本地会先经隧道拉字幕(慢且浪费回程)。
        # 前哨不可用时降级回本地隧道下载+ASR(POT+EJS)。
        if video_info.platform == "youtube":
            prefetch = self._try_vm_prefetch(video_info)
            if prefetch and prefetch.full_text:
                return prefetch
            logger.info("VM 前哨未命中, 降级本地隧道字幕/下载+ASR (POT+EJS)")
            subtitle_result = self.media_extractor.extract_subtitles(video_info)
            if subtitle_result and subtitle_result.full_text:
                return subtitle_result
            return self._transcribe_with_asr(video_info, save_audio_path)

        # 策略：auto - 优先尝试字幕（但抖音平台跳过）
        # 抖音的"字幕"只是视频描述，通常只有 hashtags，没有实际语音内容
        if policy == "auto" and video_info.platform != "douyin":
            # 尝试提取平台字幕
            subtitle_result = self.media_extractor.extract_subtitles(video_info)
            if subtitle_result and subtitle_result.full_text:
                logger.info("Successfully extracted platform subtitles")
                return subtitle_result

            logger.info("No subtitles found, checking video type...")

        # 抖音平台：直接使用 ASR
        if video_info.platform == "douyin":
            logger.info("Douyin platform: skipping subtitle extraction (hashtags only), using ASR directly")

        # 检测并处理图片视频
        image_result = self._try_image_video(video_info)
        if image_result and image_result.full_text:
            logger.info("Successfully processed as image video")
            return image_result

        # 普通 ASR 转录
        return self._transcribe_with_asr(video_info, save_audio_path)

    def _try_vm_prefetch(self, video_info: VideoInfo) -> Optional[TranscriptResult]:
        """调 VM YouTube 转写前哨(v3.5): VM 侧下载+字幕/mimo-asr, 回传仅文本。

        前哨在 VM 出海满速(12.7MB/s)处理, 避开回程隧道 82KB/s 瓶颈。
        返回 None 表示前哨不可用/失败, 由调用方降级回本地隧道下载。
        """
        base = getattr(self.config, "yt_prefetch_url", "")
        if not base:
            return None
        try:
            import requests
            # 长视频可能分片多轮 ASR, 给足超时(VM 处理, 不占本机)
            resp = requests.post(f"{base.rstrip('/')}/transcript", timeout=1500,
                                 json={"url": video_info.original_url or video_info.resolved_url})
            if resp.status_code != 200:
                logger.warning(f"VM 前哨 HTTP {resp.status_code}")
                return None
            data = resp.json()
            if not data.get("ok") or not data.get("text"):
                logger.info(f"VM 前哨无结果: {data.get('error', '')[:100]}")
                return None
            # 回填元信息
            if data.get("title") and not video_info.title:
                video_info.title = data["title"]
            if data.get("duration") and not video_info.duration:
                video_info.duration = data["duration"]
            logger.info(f"VM 前哨命中: source={data.get('source')} chars={data.get('chars')}")
            return TranscriptResult(
                segments=[], full_text=data["text"],
                source=f"vm_prefetch_{data.get('source', 'unknown')}",
                language="zh", confidence=0.9)
        except Exception as e:
            logger.warning(f"VM 前哨调用失败: {e}")
            return None

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

            # 非图片视频：保存已下载的文件供后续音频提取复用，避免二次下载
            import shutil
            _, persist_path = tempfile.mkstemp(suffix=".mp4", prefix="distill_reuse_")
            shutil.copy2(str(video_path), persist_path)
            video_info._cached_video_path = persist_path
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

            # 策略(2026-08-03 统一): 本地 MLX 首选(免费,medium 模型已预拉) → mimo 云端兜底(受预算闸门)
            # 此前 v3.4 是 mimo 优先,理由"MLX 首次依赖 HF 下载"已因 medium 模型经隧道预拉而过时。
            # 与播客链路 transcribe_audio.py 对齐,两条链路共用 zhiwei_common.asr_budget 账本。
            transcript_result = None

            # 1. 本地 MLX Whisper 首选(免费)
            if self.local_transcriber.is_available():
                logger.info("尝试本地 MLX Whisper ASR(首选)...")
                try:
                    transcript_result = self.local_transcriber.transcribe(actual_audio_path)
                    if transcript_result and transcript_result.full_text:
                        logger.info(f"本地 MLX 成功: {len(transcript_result.full_text)} 字符")
                        return transcript_result
                    logger.warning("本地 MLX 返回空, 降级 mimo 云端")
                except Exception as e:
                    logger.warning(f"本地 MLX 失败: {e}, 降级 mimo 云端")

            # 2. mimo 云端兜底(受每日预算闸门保护)
            if self.mimo_transcriber.is_available():
                try:
                    from zhiwei_common import asr_budget
                    est = asr_budget.estimate_minutes(actual_audio_path)
                    if not asr_budget.budget_ok(est):
                        logger.warning(
                            f"mimo 日预算不足(已用{asr_budget.used_today():.0f}+{est:.0f}min), 跳过云端")
                    else:
                        logger.info("降级 mimo-asr 云端 ASR...")
                        transcript_result = self.mimo_transcriber.transcribe(actual_audio_path)
                        if transcript_result and transcript_result.full_text:
                            asr_budget.budget_record(est)
                            logger.info(f"mimo-asr 成功: {len(transcript_result.full_text)} 字符")
                            return transcript_result
                        logger.warning("mimo-asr 返回空结果")
                except Exception as e:
                    logger.error(f"mimo-asr 也失败: {e}")

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
    从 zhiwei-rag 检索背景知识(通过 HTTP API,复用 core/rag_client)

    旧实现直接 import HybridRetriever,需要 shared-venv 装 lancedb/sentence_transformers,
    与 shared-venv 轻量定位冲突,且会因 zhiwei-rag/lancedb 空目录遮蔽等误判。改用 HTTP
    调 8765/search(复用 core/rag_client.py 的 RAGClient,带 health 探测 + bridge 降级),
    环境隔离,失败 try/except 降级返回空。

    Args:
        keywords: 关键词列表
        top_k: 每个关键词检索数量

    Returns:
        格式化的背景知识文本
    """
    if not keywords:
        return ""

    try:
        # core/rag_client 在 zhiwei-bot 根,补 sys.path
        bot_root = Path(__file__).parent.parent
        if str(bot_root) not in sys.path:
            sys.path.insert(0, str(bot_root))
        from core.rag_client import get_rag_client

        client = get_rag_client()
        background_parts = []

        for kw in keywords[:3]:  # 最多检索 3 个关键词
            results = client.search(kw, top_k=top_k)

            if results:
                background_parts.append(f"**{kw}** 相关资料：")
                for r in results[:2]:  # 每个关键词取前 2 条
                    source = Path(r.get("source") or "").stem or "未知来源"
                    text = (r.get("text") or r.get("raw_text") or r.get("content") or "")[:200]
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
    """知识蒸馏引擎 v2.0 - 两阶段蒸馏"""

    # ── 第一阶段：清洗 + 分类 ──
    STAGE1_SYSTEM_PROMPT = """你是转录文本清洗专家。对语音识别(ASR)产出的文本执行以下三项任务。

1. **ASR 纠错**：修正语音识别错误。常见模式：
   - 英文专有名词被音译为中文（如"靠比Y"→"ComfyUI"、"爱语言模型"→"AI语言模型"、"恰吉梯"→"ChatGPT"）
   - 同音字错误（如"悲视频"→"配视频"、"新鸟"→"新的"）
   - 只修正明显的语音误识别，保持原意不变

2. **内容分类**（按以下判据严格区分）：
   - tech_tutorial: 以特定工具/框架/代码为核心，教观众"怎么用"。关键信号：演示操作步骤、展示代码/配置、讲解特定工具的功能和参数
   - business_insight: 以市场/商业/投资判断为核心。关键信号：讨论市场规模、竞争格局、商业模式、融资、行业趋势
   - creative_workflow: 以内容创作的完整流程为核心，教观众"怎么做出作品"。关键信号：涉及多工具串联生产内容（视频/图像/音频/文案），强调工作流SOP和产出物
   - knowledge_explainer: 以概念/方法论/认知框架为核心，教观众"怎么理解/怎么学"。关键信号：讲解抽象概念、学习方法、思维模型、类比推理，不以特定工具实操为主线
   - general: 不属于以上任何一类
   注意：如果视频同时涉及"概念讲解"和"工具实操"，以主线判断——讲概念时顺带提工具→knowledge_explainer；讲工具时顺带解释原理→tech_tutorial

3. **实体提取**：列出视频中提到的关键工具、技术、人物、平台、产品名称。

严格输出合法 JSON，不要添加额外解释文字：
{"corrected_transcript": "纠错后的完整文本", "content_type": "tech_tutorial", "entities": ["工具1", "技术2"], "correction_count": 12}"""

    # ⭐ 2026-08-02 P0: Stage1 降级版提示词——仅分类+实体，不要求回显纠错稿。
    # 背景：回显型任务(1.2万字纠错稿)在 90s 超时上结构性不稳，全链失败曾
    # 静默降级 general 模板导致笔记变薄（远景能源访谈事故）。缩小任务重试。
    STAGE1_CLASSIFY_ONLY_PROMPT = """你是内容分类专家。对给定文本（可能是 ASR 转录稿）仅做分类和实体提取，不要复述/纠错原文。

内容分类判据：
- tech_tutorial: 以特定工具/框架/代码为核心
- business_insight: 以市场/商业/投资判断为核心
- creative_workflow: 以内容创作流程为核心
- knowledge_explainer: 以概念/方法论/认知框架为核心
- general: 不属于以上任何一类

严格输出合法 JSON（corrected_transcript 固定为空字符串）：
{"corrected_transcript": "", "content_type": "general", "entities": ["实体1", "实体2"], "correction_count": 0}"""

    # ── 第二阶段：按类型分析 ──
    STAGE2_PROMPTS = {
        "tech_tutorial": """你是顶级技术研究员，从视频转录中提取可直接指导工程实现的技术情报。

**评级标准**：A级=能闭环复现 | B级=关键点明确 | C级=仅供跟踪 | D级=无实质内容

**观点论据收集**：author_claims 只忠实收集作者明确表达的观点及其依据（不做评判、不评级）；无依据的观点也要收录，evidences 留空数组。

**输出 JSON**（严格合法 JSON，无额外文字）：
{
  "title": "主张式标题（观点/价值，非描述）",
  "core_insight": "一句话核心洞察",
  "content_tier": "A/B/C/D",
  "key_points": [{"timestamp": "MM:SS", "insight": "技术锚点"}],
  "technical_reconstruction": {
    "architecture": "底层架构/原理分析",
    "tooling": "工具链：名称、版本、用途",
    "metrics": "量化数据：性能/成本/效率指标",
    "pitfalls": "避坑指南：常见问题和解决方案"
  },
  "summary": "150-300字深度总结：能得到什么，不是讲了什么",
  "action_items": ["具体可执行的工程建议"],
  "implementation_guide": {
    "difficulty": "简单/中等/困难",
    "prerequisites": "环境依赖",
    "steps": ["复现步骤"],
    "key_code_logic": "核心逻辑/伪代码"
  },
  "tags": ["3-5个具体标签"],
  "author_claims": [{"claim": "作者明确主张的观点（一句话）", "timestamp": "MM:SS", "evidences": [{"content": "支撑该观点的论据", "type": "数据|推导|引用|类比|经验|断言", "timestamp": "MM:SS"}]}],
  "related_concepts": ["关联知识概念"]
}""",

        "business_insight": """你是资深商业分析师，从视频转录中提取商业洞察和决策情报。

**评级标准**：A级=有数据支撑的深度分析 | B级=有明确观点和逻辑 | C级=仅信息汇总 | D级=无实质内容

**观点论据收集**：author_claims 只忠实收集作者明确表达的观点及其依据（不做评判、不评级）；无依据的观点也要收录，evidences 留空数组。

**输出 JSON**（严格合法 JSON，无额外文字）：
{
  "title": "主张式标题",
  "core_insight": "一句话核心判断",
  "content_tier": "A/B/C/D",
  "key_points": [{"timestamp": "MM:SS", "insight": "关键判断"}],
  "market_analysis": {
    "market_size": "市场规模/增长数据",
    "competitive_landscape": "竞争格局分析",
    "opportunities": "机会评估",
    "risks": "风险因素"
  },
  "summary": "150-300字商业摘要",
  "action_items": ["决策建议"],
  "tags": ["3-5个标签"],
  "author_claims": [{"claim": "作者明确主张的观点（一句话）", "timestamp": "MM:SS", "evidences": [{"content": "支撑该观点的论据", "type": "数据|推导|引用|类比|经验|断言", "timestamp": "MM:SS"}]}],
  "related_concepts": ["关联概念"]
}""",

        "creative_workflow": """你是创意工作流专家，从视频转录中拆解可复制的创作流程和工具链。

**评级标准**：A级=完整可复现的SOP | B级=流程清晰但需补充细节 | C级=仅概述 | D级=无实质内容

**观点论据收集**：author_claims 只忠实收集作者明确表达的观点及其依据（不做评判、不评级）；无依据的观点也要收录，evidences 留空数组。

**输出 JSON**（严格合法 JSON，无额外文字）：
{
  "title": "主张式标题",
  "core_insight": "一句话核心价值",
  "content_tier": "A/B/C/D",
  "key_points": [{"timestamp": "MM:SS", "insight": "流程节点"}],
  "workflow_sop": {
    "pipeline": "输入→处理→输出 全流程概述",
    "tool_chain": [{"tool": "工具名", "role": "用途", "alternatives": "替代方案"}],
    "steps": ["详细步骤（可直接按步骤操作）"],
    "cost_estimate": "成本/时间估算",
    "quality_tips": "提升产出质量的关键技巧"
  },
  "summary": "150-300字摘要",
  "action_items": ["立即可做的事"],
  "tags": ["3-5个标签"],
  "author_claims": [{"claim": "作者明确主张的观点（一句话）", "timestamp": "MM:SS", "evidences": [{"content": "支撑该观点的论据", "type": "数据|推导|引用|类比|经验|断言", "timestamp": "MM:SS"}]}],
  "related_concepts": ["关联概念"]
}""",

        "knowledge_explainer": """你是知识蒸馏专家，从视频转录中提取核心概念和认知升级点。

**评级标准**：A级=深刻且有原创性 | B级=清晰系统的讲解 | C级=基础科普 | D级=无实质内容

**观点论据收集**：author_claims 只忠实收集作者明确表达的观点及其依据（不做评判、不评级）；无依据的观点也要收录，evidences 留空数组。

**输出 JSON**（严格合法 JSON，无额外文字）：
{
  "title": "主张式标题",
  "core_insight": "一句话核心认知",
  "content_tier": "A/B/C/D",
  "key_points": [{"timestamp": "MM:SS", "insight": "认知锚点"}],
  "knowledge_framework": {
    "core_concepts": ["核心概念及定义"],
    "mental_model_shift": "认知升级点：与常见理解的差异",
    "learning_path": "推荐学习路径",
    "further_reading": "延伸阅读方向"
  },
  "summary": "150-300字知识摘要",
  "action_items": ["学习建议"],
  "tags": ["3-5个标签"],
  "author_claims": [{"claim": "作者明确主张的观点（一句话）", "timestamp": "MM:SS", "evidences": [{"content": "支撑该观点的论据", "type": "数据|推导|引用|类比|经验|断言", "timestamp": "MM:SS"}]}],
  "related_concepts": ["关联概念"]
}""",

        "general": """你是内容分析师，从视频转录中提取关键信息。

**输出 JSON**（严格合法 JSON，无额外文字）：
{
  "title": "主张式标题",
  "core_insight": "一句话核心内容",
  "content_tier": "A/B/C/D",
  "key_points": [{"timestamp": "MM:SS", "insight": "要点"}],
  "summary": "100-200字摘要",
  "action_items": ["建议"],
  "tags": ["3-5个标签"],
  "related_concepts": ["关联概念"]
}"""
    }

    STAGE2_USER_TEMPLATE = """视频信息：
- 平台：{platform}
- 时长：{duration}秒
- 作者：{author}
- 内容类型：{content_type}
- 识别实体：{entities}

转录文本（已纠错）：
{transcript}

{background_section}

请深度分析并输出 JSON。"""

    # 保留旧版 prompt 作为兼容回退
    SYSTEM_PROMPT = """你是一个顶级的技术研究员与情报分析师，擅长从视频内容中"脱水"出极高密度的技术情报。
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

请提取知识点，并特别关注 **"如何复现视频中的方案"**。
输出 JSON 格式结果。"""

    def __init__(self, config: AppConfig):
        self.config = config
        self._init_client()

    def _init_client(self):
        """初始化统一 LLM 客户端"""
        try:
            from zhiwei_common.llm import llm_client
        except ImportError:
            # zhiwei_common 未以 installed 包形式存在时的兜底（2026-07-31: 各 venv 已 editable 安装，hack 已去）
            from zhiwei_common.llm import llm_client

        # zhiwei_common.llm.LLMClient.call(role, message, system_prompt, timeout)
        # -> (success, content)，与下方 Stage1/Stage2 共 3 处调用点接口一致，
        # 无需适配器桥接（role: format/research 均在 ROLE_PROMPTS 中）。
        self.llm_client = llm_client
        logger.info("Using unified LLM client (v2.0: two-stage distillation)")

    def _stage1_clean_and_classify(self, transcript_text: str) -> dict:
        """第一阶段：ASR 纠错 + 内容分类 + 实体提取（format role, 轻量快速）"""
        logger.info("Stage 1: ASR correction + content classification (format role)")

        user_msg = f"请处理以下 ASR 转录文本：\n\n{transcript_text}"

        try:
            success, content = self.llm_client.call_by_task(
                task="classify",
                message=user_msg,
                system_prompt=self.STAGE1_SYSTEM_PROMPT,
                timeout=90
            )

            if success:
                json_match = re.search(r'\{[\s\S]*\}', content)
                if json_match:
                    result = json.loads(json_match.group())
                    corrections = result.get("correction_count", 0)
                    content_type = result.get("content_type", "general")
                    entities = result.get("entities", [])
                    logger.info(f"Stage 1 complete: type={content_type}, corrections={corrections}, entities={len(entities)}")
                    return result
        except (json.JSONDecodeError, Exception) as e:
            logger.warning(f"Stage 1 parse failed: {e}")

        return {
            "corrected_transcript": transcript_text,
            "content_type": "general",
            "entities": [],
            "correction_count": 0,
            "_llm_failed": True,  # ⭐ P0: 标记 LLM 调用失败（区别于真判为 general）
        }

    def _stage1_classify_only(self, text: str) -> Optional[dict]:
        """⭐ P0 (2026-08-02): Stage1 降级版——仅分类+实体（小输入，防回显超时）。

        成功返回含 content_type 的 dict；失败返回 None。
        corrected_transcript 为空串，distill 的防御逻辑会自动改用原文。
        """
        try:
            success, content = self.llm_client.call_by_task(
                task="classify",
                message=f"请处理以下文本：\n\n{text}",
                system_prompt=self.STAGE1_CLASSIFY_ONLY_PROMPT,
                timeout=60,
            )
            if success:
                m = re.search(r'\{[\s\S]*\}', content)
                if m:
                    result = json.loads(m.group())
                    logger.info(f"Stage 1 降级重试成功: type={result.get('content_type')}")
                    return result
        except Exception as e:
            logger.warning(f"Stage 1 降级重试异常: {e}")
        return None

    def _get_background(self, transcript_text: str) -> str:
        """RAG 背景增强"""
        try:
            keywords = extract_keywords_from_transcript(transcript_text)
            if keywords:
                logger.info(f"[RAG] 提取关键词: {keywords}")
                background_knowledge = retrieve_background_knowledge(keywords)
                if background_knowledge:
                    return f"**背景知识（来自本地知识库）：**\n{background_knowledge}"
        except Exception as e:
            logger.warning(f"[RAG] 背景增强失败: {e}")
        return ""

    def _chunk_transcript(self, transcript: TranscriptResult, max_chars: int = 8000) -> list:
        """长视频分段：超出 max_chars 时按时间戳均分"""
        full_text = transcript.to_text()
        if len(full_text) <= max_chars:
            return [full_text]

        segments = transcript.segments
        if not segments:
            # 无时间戳，按字符均分
            n_chunks = (len(full_text) // max_chars) + 1
            chunk_size = len(full_text) // n_chunks
            return [full_text[i*chunk_size:(i+1)*chunk_size] for i in range(n_chunks)]

        # 按时间戳均分为 2-3 段
        n_chunks = min(3, (len(full_text) // max_chars) + 1)
        chunk_size = len(segments) // n_chunks
        chunks = []
        for i in range(n_chunks):
            start = i * chunk_size
            end = (i + 1) * chunk_size if i < n_chunks - 1 else len(segments)
            chunk_text = " ".join(s.text for s in segments[start:end])
            if segments[start:end]:
                time_range = f"[{segments[start].start:.0f}s - {segments[end-1].end:.0f}s]"
                chunks.append(f"{time_range}\n{chunk_text}")
            else:
                chunks.append(chunk_text)
        logger.info(f"Transcript split into {len(chunks)} chunks ({len(full_text)} chars total)")
        return chunks

    def distill(self, video_info: VideoInfo, transcript: TranscriptResult,
                extra_context: str = "") -> DistilledKnowledge:
        """两阶段知识蒸馏 v2.0

        Stage 1: ASR 纠错 + 内容分类（format role / MiniMax-M2.5）
        Stage 2: 按类型深度分析（research role / qwen3.7-plus）

        Args:
            extra_context: 额外上下文(如 --vision 的视觉信息段落),附入 Stage 2 prompt
        """
        transcript_text = transcript.to_text()
        logger.info(f"Distilling {len(transcript_text)} chars transcript (v2.0 two-stage)")

        # ── 第一阶段：清洗 + 分类 ──
        # ⭐ v70.3 (2026-08-02): Stage1 全面改为「仅分类」主路径，不再回显纠错稿。
        # 证据链：百炼 coding plan 在长输出任务上系统性装死（90s 零字节，全系模型
        # 裸 HTTP 复现，且早于任何代码改动）；分类/实体只需 3k 样本，快 10 倍；
        # Stage2 大模型原生抗 ASR 噪声（纠错本属锦上添花）。
        is_vlm = transcript.source and "vlm" in transcript.source
        stage1_result = self._stage1_classify_only(transcript_text[:3000])
        if not stage1_result:
            logger.warning("⚠️ Stage 1 仅分类调用失败，接受 general 模板（笔记将缺少类型化深度章节）")
            stage1_result = {"corrected_transcript": "", "content_type": "general",
                             "entities": [], "correction_count": 0}

        content_type = stage1_result.get("content_type", "general")
        entities = stage1_result.get("entities", [])

        # VLM 源不需要纠错，直接用原始文本
        if is_vlm:
            corrected_text = transcript_text
        else:
            corrected_text = stage1_result.get("corrected_transcript", transcript_text)

        # 防御：如果纠错后文本明显短于原文（LLM 截断），拼接原文剩余部分
        if not is_vlm and len(corrected_text) < len(transcript_text) * 0.7:
            logger.warning(f"Stage 1 truncated output ({len(corrected_text)}/{len(transcript_text)} chars), appending remainder")
            corrected_text = corrected_text + transcript_text[len(corrected_text):]

        # 更新 video_info 的 content_type 供后续使用
        video_info.description = content_type

        # ── RAG 背景增强 ──
        background_section = self._get_background(corrected_text)

        # ── 视觉信息增强 (--vision) ──
        if extra_context:
            background_section = (background_section + "\n\n" + extra_context).strip()

        # ── 第二阶段：按类型深度分析 ──
        stage2_prompt = self.STAGE2_PROMPTS.get(content_type, self.STAGE2_PROMPTS["general"])

        # 长视频分段处理（使用纠错后文本）
        corrected_len = len(corrected_text)
        # 默认整段；corrected_len<=8000 时不分块，避免 chunks/all_key_points 未定义
        # （否则下方 2634 行 `if len(chunks) > 1` 会 NameError）
        chunks = [corrected_text]
        all_key_points = []
        if corrected_len > 8000:
            # 按字符均分为 2-3 段
            n_chunks = min(3, (corrected_len // 8000) + 1)
            chunk_size = corrected_len // n_chunks
            chunks = [corrected_text[i*chunk_size:(i+1)*chunk_size] for i in range(n_chunks)]
            logger.info(f"Transcript split into {n_chunks} chunks ({corrected_len} chars)")
            for i, chunk in enumerate(chunks[:-1]):
                logger.info(f"Stage 2: analyzing chunk {i+1}/{n_chunks}")
                chunk_prompt = f"提取此视频片段的 key_points（只需时间戳和洞察点）：\n{chunk}\n\n输出 JSON: {{\"key_points\": [{{\"timestamp\": \"MM:SS\", \"insight\": \"...\"}}]}}"
                try:
                    ok, resp = self.llm_client.call_by_task(task="classify", message=chunk_prompt, timeout=60)
                    if ok:
                        m = re.search(r'\{[\s\S]*\}', resp)
                        if m:
                            pts = json.loads(m.group()).get("key_points", [])
                            all_key_points.extend(pts)
                except Exception:
                    pass

            analysis_text = corrected_text[:8000]
            if all_key_points:
                prev_points = "\n".join(f"- [{p.get('timestamp','')}] {p.get('insight','')}" for p in all_key_points)
                background_section += f"\n\n**前段已提取的要点：**\n{prev_points}"
        else:
            analysis_text = corrected_text

        user_prompt = self.STAGE2_USER_TEMPLATE.format(
            platform=video_info.platform,
            duration=video_info.duration,
            author=video_info.author or "未知",
            content_type=content_type,
            entities=", ".join(entities[:15]),
            transcript=analysis_text,
            background_section=background_section
        )

        logger.info(f"Stage 2: deep analysis with research role (content_type={content_type})")

        try:
            success, content = self.llm_client.call_by_task(
                task="deep_analysis",
                message=user_prompt,
                system_prompt=stage2_prompt,
                timeout=240  # deepseek-v4-pro 42s on 5k chars, ample margin
            )

            if success:
                result = self._parse_response(content)
                # ⭐ 2026-08-05: JSON 畸形时换 kimi-k2.5 重试一次（与主力 deepseek-v4-pro 不同供应商）
                # minimax-m3 商业类内容 JSON 畸形 3/3（引号未转义，非网络问题），已弃用为主力
                if result.title == "解析失败":
                    logger.warning("Stage 2 JSON 解析失败，切换 kimi-k2.5 (Coding Plan) 重试")
                    try:
                        retry_ok, retry_content = self.llm_client.call(
                            "research", user_prompt,
                            system_prompt=stage2_prompt,
                            timeout=240, prefer_api="coding_plan"
                        )
                        if retry_ok:
                            retry_result = self._parse_response(retry_content)
                            if retry_result.title != "解析失败":
                                logger.info("Stage 2 重试成功 (kimi-k2.5)")
                                result = retry_result
                    except Exception as re_:
                        logger.error(f"Stage 2 重试异常: {re_}")
                # 补充分段提取的 key_points
                if len(chunks) > 1 and all_key_points:
                    existing_ts = {p.get("timestamp") for p in result.key_points}
                    for p in all_key_points:
                        if p.get("timestamp") not in existing_ts:
                            result.key_points.append(p)
                    result.key_points.sort(key=lambda x: x.get("timestamp", "99:99"))
                return result
            else:
                logger.error(f"Stage 2 failed: {content}")
                return self._fallback_distill(video_info, transcript)

        except Exception as e:
            logger.error(f"Stage 2 error: {e}")
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
        except json.JSONDecodeError:
            # ⭐ 2026-08-05: JSON 容错——LLM 常在字符串内输出未转义换行/尾逗号
            # （minimax-m3 实测会产出此类畸形 JSON，此前直接落"解析失败"模板）
            try:
                data = json.loads(json_str, strict=False)  # 允许字符串内控制字符
                logger.info("JSON 容错解析成功 (strict=False)")
            except json.JSONDecodeError:
                try:
                    repaired = re.sub(r',\s*([}\]])', r'\1', json_str)  # 去尾逗号
                    data = json.loads(repaired, strict=False)
                    logger.info("JSON 修复解析成功 (去尾逗号)")
                except json.JSONDecodeError as e2:
                    logger.error(f"JSON parse error: {e2}")
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

        # 按内容类型取对应的分析字段，统一存入 technical_reconstruction
        type_analysis = (
            data.get("technical_reconstruction")
            or data.get("knowledge_framework")
            or data.get("market_analysis")
            or data.get("workflow_sop")
            or {"architecture": "未提取", "tooling": "未提取", "metrics": "未提取", "pitfalls": "未提取"}
        )
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
            technical_reconstruction=type_analysis,
            implementation_guide=data.get("implementation_guide", {}),
            author_claims=data.get("author_claims", [])
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
    """Markdown 笔记生成与写入 v2.0 — 按 content_type 差异化输出"""

    FRONTMATTER = '''---
title: "{title}"
source_url: "{source_url}"
date: {date}
type: video_distill
content_type: {content_type}
tags: [{tags}]
tier: {content_tier}
asr_source: "{asr_source}"
related: [{related_concepts}]
rag: {rag_flag}
---'''

    HEADER = '''
# {title}

> **内容等级：{tier_display}** | **分类：{content_type_label}**

## 核心洞察
{core_insight}
'''

    SECTION_TECH = '''
## 技术情报重建
### 架构与原理
{tech_arch}

### 工具链
{tech_tools}

### 量化指标
{tech_metrics}

### 避坑指南
{tech_pitfalls}
'''

    SECTION_BUSINESS = '''
## 商业洞察
### 市场规模与数据
{market_size}

### 竞争格局
{competitive_landscape}

### 机会评估
{opportunities}

### 风险因素
{risks}
'''

    SECTION_WORKFLOW = '''
## 创作工作流 SOP
### 全流程概述
{pipeline}

### 工具链
{tool_chain}

### 详细步骤
{steps}

### 成本与时间
{cost_estimate}

### 质量提升技巧
{quality_tips}
'''

    SECTION_KNOWLEDGE = '''
## 知识框架
### 核心概念
{core_concepts}

### 认知升级点
{mental_model_shift}

### 学习路径
{learning_path}

### 延伸阅读
{further_reading}
'''

    SECTION_CLAIMS = '''
## 作者观点与论据
{claims_block}
'''

    SECTION_IMPL = '''
## 复现指南
- **难度**：{impl_difficulty}
- **环境依赖**：{impl_prerequisites}
- **核心逻辑**：
```text
{impl_code_logic}
```
- **步骤**：
{impl_steps}
'''

    SECTION_VISION = '''
## 关键画面与图表
{vision_frames}
'''

    FOOTER = '''
## 关键锚点
{key_points}

## 摘要
{summary}

## 行动建议
{action_items_section}

{references_section}

## 关联资产
- 原始音频：[播放]({asset_audio_url})
- 转录全文：[查看]({asset_transcript_url})

---
- 来源：{platform} / {author}
- 原始链接：[查看]({source_url})

<details>
<summary>ASR 转录原文</summary>

{full_transcript}

</details>

---
> 由知微系统 v2.0 生成
'''

    @staticmethod
    def _format_steps(steps):
        """格式化步骤列表，去除 LLM 产出的重复编号前缀"""
        if not steps:
            return "参考摘要"
        if not isinstance(steps, list):
            return str(steps)
        lines = []
        for i, s in enumerate(steps):
            s = re.sub(r'^\d+[\.\)、]\s*', '', str(s).strip())
            lines.append(f"{i+1}. {s}")
        return "\n".join(lines)

    CONTENT_TYPE_LABELS = {
        "tech_tutorial": "技术教程",
        "business_insight": "商业洞察",
        "creative_workflow": "创意工作流",
        "knowledge_explainer": "知识科普",
        "general": "综合内容"
    }

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir

    def _build_claims_section(self, knowledge: DistilledKnowledge) -> str:
        """作者观点与论据区（忠实提取，不做评判；无此数据时静默省略）"""
        claims = getattr(knowledge, "author_claims", None) or []
        blocks = []
        for i, c in enumerate(claims, 1):
            claim = str(c.get("claim", "")).strip()
            if not claim:
                continue
            ts = c.get("timestamp", "")
            head = f"### C{i} · {claim}" + (f" [{ts}]" if ts else "")
            lines = [head]
            evs = c.get("evidences") or []
            if evs:
                for e in evs:
                    etype = e.get("type", "论据")
                    ets = e.get("timestamp", "")
                    ts_s = f" [{ets}]" if ets else ""
                    lines.append(f"- （{etype}）{ts_s} {e.get('content', '')}")
            else:
                lines.append("- （作者未给出依据）")
            blocks.append("\n".join(lines))
        if not blocks:
            return ""
        return self.SECTION_CLAIMS.format(claims_block="\n\n".join(blocks))

    def _build_type_section(self, knowledge: DistilledKnowledge, content_type: str) -> str:
        """根据 content_type 构建差异化中段"""
        tr = knowledge.technical_reconstruction or {}

        if content_type == "tech_tutorial":
            impl = getattr(knowledge, 'implementation_guide', {}) or {}
            steps = impl.get('steps', [])
            impl_steps = self._format_steps(steps)
            section = self.SECTION_TECH.format(
                tech_arch=tr.get("architecture", "未提取"),
                tech_tools=tr.get("tooling", "未提取"),
                tech_metrics=tr.get("metrics", "未提取"),
                tech_pitfalls=tr.get("pitfalls", "未提取")
            )
            section += self.SECTION_IMPL.format(
                impl_difficulty=impl.get('difficulty', '未知'),
                impl_prerequisites=impl.get('prerequisites', '见摘要'),
                impl_code_logic=impl.get('key_code_logic', '见摘要'),
                impl_steps=impl_steps
            )
            return section

        elif content_type == "business_insight":
            ma = tr if "market_size" in tr else knowledge.__dict__.get("market_analysis", tr)
            return self.SECTION_BUSINESS.format(
                market_size=ma.get("market_size", "未提取"),
                competitive_landscape=ma.get("competitive_landscape", "未提取"),
                opportunities=ma.get("opportunities", "未提取"),
                risks=ma.get("risks", "未提取")
            )

        elif content_type == "creative_workflow":
            wf = tr if "pipeline" in tr else knowledge.__dict__.get("workflow_sop", tr)
            tool_chain = wf.get("tool_chain", [])
            if isinstance(tool_chain, list):
                tc_str = "\n".join(f"- **{t.get('tool',t)}**：{t.get('role','')}" if isinstance(t, dict) else f"- {t}" for t in tool_chain)
            else:
                tc_str = str(tool_chain)
            steps = wf.get("steps", [])
            steps_str = self._format_steps(steps)
            section = self.SECTION_WORKFLOW.format(
                pipeline=wf.get("pipeline", "未提取"),
                tool_chain=tc_str,
                steps=steps_str,
                cost_estimate=wf.get("cost_estimate", "未提取"),
                quality_tips=wf.get("quality_tips", "未提取")
            )
            impl = getattr(knowledge, 'implementation_guide', {}) or {}
            if impl.get('steps'):
                impl_steps = self._format_steps(impl['steps'])
                section += self.SECTION_IMPL.format(
                    impl_difficulty=impl.get('difficulty', '未知'),
                    impl_prerequisites=impl.get('prerequisites', '见摘要'),
                    impl_code_logic=impl.get('key_code_logic', '见摘要'),
                    impl_steps=impl_steps
                )
            return section

        elif content_type == "knowledge_explainer":
            kf = tr if "core_concepts" in tr else knowledge.__dict__.get("knowledge_framework", tr)
            concepts = kf.get("core_concepts", [])
            if isinstance(concepts, list):
                concepts_str = "\n".join(f"- {c}" for c in concepts)
            else:
                concepts_str = str(concepts)
            return self.SECTION_KNOWLEDGE.format(
                core_concepts=concepts_str,
                mental_model_shift=kf.get("mental_model_shift", "未提取"),
                learning_path=kf.get("learning_path", "未提取"),
                further_reading=kf.get("further_reading", "未提取")
            )

        return ""

    def _build_vision_section(self, visual_frames: list, asset_dir: Path, asset_folder_name: str) -> str:
        """拷贝视觉帧到 Assets/{folder}/frames/ 并生成「关键画面与图表」章节(--vision)"""
        if not visual_frames:
            return ""
        import shutil
        frames_dir = asset_dir / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)
        blocks = []
        for f in visual_frames:
            if not f.description or f.description.startswith("["):
                continue
            info = VisionAnalyzer.parse_frame_json(f.description)
            if info.get("type") == "scene" and not info.get("key_insights"):
                continue
            src = Path(f.path)
            if not src.exists():
                continue
            dest_name = f"frame_{f.index:03d}.jpg"
            try:
                shutil.copy2(str(src), str(frames_dir / dest_name))
            except OSError as e:
                logger.warning(f"[vision] 帧拷贝失败: {e}")
                continue
            ts = f"{int(f.timestamp // 60):02d}:{int(f.timestamp % 60):02d}"
            rel_path = f"Assets/{asset_folder_name}/frames/{dest_name}"
            title = info.get("title") or f"画面 {f.index + 1}"
            lines = [f"### [{ts}] {title}", "", f"![{title}]({rel_path})", ""]
            if info.get("data_summary"):
                lines.append(f"- **数据**: {info['data_summary']}")
            for ins in info.get("key_insights", []):
                lines.append(f"- {ins}")
            if info.get("description"):
                lines.append(f"- {info['description']}")
            blocks.append("\n".join(lines))
        if not blocks:
            return ""
        return self.SECTION_VISION.format(vision_frames="\n\n".join(blocks))

    def write(self, video_info: VideoInfo, transcript: TranscriptResult,
              knowledge: DistilledKnowledge, noise_tags: list[str],
              visual_frames: list = None) -> Path:
        """生成并写入 Markdown 文件，按 content_type 差异化输出"""
        date_str = datetime.now().strftime("%Y-%m-%d")
        tags_str = ", ".join(f'"{tag}"' for tag in knowledge.tags)
        safe_title = re.sub(r'[<>:"/\\|?*]', '', knowledge.title)[:50]
        asset_folder_name = f"{date_str}_{safe_title}"
        asset_dir = self.output_dir / "Assets" / asset_folder_name
        asset_dir.mkdir(parents=True, exist_ok=True)

        transcript_filename = "transcript_full.txt"
        (asset_dir / transcript_filename).write_text(transcript.to_text(), encoding="utf-8")
        asset_audio_url = f"Assets/{asset_folder_name}/audio.mp3"
        asset_transcript_url = f"Assets/{asset_folder_name}/{transcript_filename}"

        tier_labels = {"A": "A 深度干货", "B": "B 有价值", "C": "C 浅层内容", "D": "D 信息稀薄"}
        tier_display = tier_labels.get(knowledge.content_tier, "未评估")

        content_type = getattr(video_info, 'description', '') or "general"
        content_type_label = self.CONTENT_TYPE_LABELS.get(content_type, "综合内容")

        key_points_str = "\n".join(f"- **[{kp.get('timestamp','')}]** {kp.get('insight','')}" for kp in knowledge.key_points)
        items_str = "\n".join(f"- [ ] {item}" for item in knowledge.action_items) if knowledge.action_items else ""
        refs_str = "\n".join(f"- {ref}" for ref in knowledge.references) if knowledge.references else ""
        references_section = f"## 参考资料\n{refs_str}" if refs_str else ""
        related_str = ", ".join(f'"{c}"' for c in knowledge.related_concepts) if knowledge.related_concepts else ""

        # 组装：frontmatter + header + 类型化中段 + 视觉章节(--vision) + footer
        # v3.0: rag 标志按质量分级——A/B 级直接入库,C/D 级由 DKI 隔离(防 ASR 噪声污染知识库)
        rag_flag = "true" if knowledge.content_tier in ("A", "B") else "false"
        parts = [
            self.FRONTMATTER.format(
                title=knowledge.title, source_url=video_info.original_url,
                date=date_str, tags=tags_str, content_tier=knowledge.content_tier,
                asr_source=transcript.source, related_concepts=related_str,
                content_type=content_type, rag_flag=rag_flag
            ),
            self.HEADER.format(
                title=knowledge.title, tier_display=tier_display,
                content_type_label=content_type_label,
                core_insight=knowledge.core_insight
            ),
            self._build_claims_section(knowledge),
            self._build_type_section(knowledge, content_type),
            self._build_vision_section(visual_frames or [], asset_dir, asset_folder_name),
            self.FOOTER.format(
                key_points=key_points_str, summary=knowledge.summary,
                action_items_section=items_str, references_section=references_section,
                asset_audio_url=asset_audio_url, asset_transcript_url=asset_transcript_url,
                platform=video_info.platform, author=video_info.author or "未知",
                source_url=video_info.original_url, full_transcript=transcript.to_text()
            )
        ]

        content = "\n".join(parts)
        filename = f"{date_str}_{safe_title}.md"
        output_path = self.output_dir / filename
        output_path.write_text(content, encoding="utf-8")
        logger.info(f"Markdown saved to: {output_path}")
        return output_path


# ============================================================================
# 主程序入口
# ============================================================================

def _trigger_rag_ingest(note_path) -> None:
    """触发 zhiwei-rag 增量入库(fire-and-forget, v3.0)

    红线: 入库必须用 zhiwei-rag/venv(本 venv 无 lancedb)。--no-archive 保持笔记在 Vault 原路径。
    """
    try:
        rag_python = Path.home() / "zhiwei-rag" / "venv" / "bin" / "python"
        ingest_script = Path.home() / "zhiwei-rag" / "scripts" / "ingest_incremental.py"
        if not rag_python.exists() or not ingest_script.exists():
            logger.warning(f"[RAG] 入库环境缺失，跳过: {note_path}")
            return
        # 入库完成后刷新检索索引（2026-08-01: 消除 lance MVCC 快照导致的搜索断层）
        cmd = (f'"{rag_python}" "{ingest_script}" --file "{note_path}" --no-archive '
               f'&& curl -s -X POST http://localhost:8765/admin/refresh >/dev/null')
        subprocess.Popen(
            ["/bin/bash", "-c", cmd],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        logger.info(f"[RAG] 已触发增量入库: {Path(note_path).name}")
    except Exception as e:
        logger.warning(f"[RAG] 触发入库失败(不影响主流程): {e}")


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
            if getattr(args, 'json', False):
                print(json.dumps({"status": "skipped", "output_path": str(output_path),
                                  "title": record.get('title', '')}, ensure_ascii=False))
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

    vision_mode = getattr(args, 'vision', False)
    provider = None
    try:
        provider = TranscriptProvider(config, cookies_browser, cookies_file)

        # 预估资产保存路径（基于 URL 解析出的 Title，如果没有则生成 ID）
        date_str = datetime.now().strftime("%Y-%m-%d")
        raw_title = video_info.title or f"Video_{video_id or 'unknown'}"
        safe_title = re.sub(r'[<>:"/\\|?*]', '', raw_title)[:50]
        asset_dir = config.output_dir / "Assets" / f"{date_str}_{safe_title}"
        asset_dir.mkdir(parents=True, exist_ok=True)
        save_audio_path = asset_dir / "audio" # 最终后缀由 extract_audio 补全

        # ⭐ v3.0: --vision 重跑时优先用缓存转写,免二次下载音频/ASR
        transcript = None
        if vision_mode:
            cached = store.get_cached_transcript(video_info.resolved_url, video_id=video_id)
            if cached:
                cached_text, cached_engine = cached
                transcript = TranscriptResult(
                    segments=[], full_text=cached_text,
                    source=f"{cached_engine}(cached)", language="zh", confidence=0.9)
                logger.info(f"[vision] 命中转写缓存({len(cached_text)} 字符, engine={cached_engine}),跳过 ASR")
        if transcript is None:
            transcript = provider.get_transcript(video_info, save_audio_path=save_audio_path)
    except Exception as e:
        error_type, error_msg = classify_error(e)
        error_json = json.dumps({"error_type": error_type.value, "error_message": error_msg})
        print(error_json, file=sys.stderr)
        logger.error(f"获取转录失败: {e}")
        return 1

    if not transcript.full_text:
        err_msg = transcript.error_details or "无法获取转录文本 (ASR 返回空或识别失败)"
        error_json = json.dumps({"error_type": VideoErrorType.ASR_FAILED.value, "error_message": err_msg})
        print(error_json, file=sys.stderr)
        logger.error(f"Failed to get transcript: {err_msg}")
        # 清理空资产目录
        if asset_dir.exists() and not any(asset_dir.iterdir()):
            asset_dir.rmdir()
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

    # ⭐ v3.0: --vision 视觉分析(抽帧 + VLM),产出并入蒸馏 prompt 与笔记章节
    visual_frames = []
    visual_context = ""
    if vision_mode:
        logger.info("Step 4a: Vision analysis (--vision)")
        analyzer = VisionAnalyzer(config)
        visual_frames = analyzer.analyze(video_info, provider.media_extractor)
        visual_context = VisionAnalyzer.build_visual_context(visual_frames)
        if visual_context:
            logger.info(f"[vision] 视觉信息段落: {len(visual_context)} 字符")
        else:
            logger.info("[vision] 无有效视觉信息(可能无图表画面)")

    # ⭐ 2026-08-04 P1.3: 用户指令注入(代称映射/还原指令),与视觉信息同通道并入蒸馏 prompt
    instruction_context = ""
    _instruction_file = getattr(args, 'instruction_file', None)
    if _instruction_file:
        try:
            _inst_text = Path(_instruction_file).expanduser().read_text(encoding='utf-8').strip()
            if _inst_text:
                instruction_context = (
                    "**用户指定背景/还原指令（最高优先级）：**\n"
                    f"{_inst_text}\n"
                    "请在分析与摘要中严格执行上述还原与映射。")
                logger.info(f"[instruction] 用户指令段落: {len(instruction_context)} 字符")
        except Exception as e:
            logger.warning(f"读取 instruction-file 失败: {e}")

    distiller = KnowledgeDistiller(config)
    try:
        _extra = (visual_context + "\n\n" + instruction_context).strip() if (visual_context or instruction_context) else ""
        knowledge = distiller.distill(video_info, transcript, extra_context=_extra)
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

    # 5. 智能 Wikilinks — 用 RAG 验证 related_concepts
    logger.info("=" * 50)
    logger.info("Step 5: Validating wikilinks via RAG")
    original_count = len(knowledge.related_concepts)
    try:
        validated_links = []
        for concept in knowledge.related_concepts[:10]:
            bg = retrieve_background_knowledge([concept])
            if bg and len(bg) > 10:
                validated_links.append(concept)
        if validated_links:
            knowledge.related_concepts = validated_links
            logger.info(f"Validated {len(validated_links)}/{original_count} wikilinks via RAG")
        else:
            logger.info("No wikilinks matched RAG, keeping as plain concepts")
    except Exception as e:
        logger.warning(f"Wikilink validation skipped: {e}")

    # 6. 生成 Markdown
    logger.info("=" * 50)
    logger.info("Step 6: Writing Markdown")
    writer = MarkdownWriter(config.output_dir)
    output_path = writer.write(video_info, transcript, knowledge, noise_tags,
                               visual_frames=visual_frames)

    # 清理视觉帧临时目录(帧已拷入 Assets)
    if visual_frames:
        import shutil
        try:
            tmp_dir = Path(visual_frames[0].path).parent
            if tmp_dir.name.startswith("vision_frames_"):
                shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass

    # 7. RAG 闭环 — A/B 级笔记复制到 JD 目录
    if knowledge.content_tier in ("A", "B"):
        logger.info("=" * 50)
        logger.info(f"Step 7: RAG promotion (tier {knowledge.content_tier})")
        try:
            import requests as req
            classify_resp = req.post(
                "http://localhost:8766/classify",
                json={"title": knowledge.title, "content": knowledge.summary[:500]},
                timeout=5
            )
            if classify_resp.status_code == 200:
                jd = classify_resp.json()
                jd_dir = Path(config.output_dir).parent.parent / jd.get("jd_dir", "")
                if jd_dir.exists():
                    import shutil
                    dest = jd_dir / output_path.name
                    shutil.copy2(str(output_path), str(dest))
                    # 在副本中标记 rag: true
                    dest_content = dest.read_text(encoding="utf-8")
                    dest_content = dest_content.replace("rag: false", "rag: true", 1)
                    dest.write_text(dest_content, encoding="utf-8")
                    logger.info(f"Promoted to JD directory: {dest}")
                else:
                    logger.warning(f"JD directory not found: {jd_dir}")
            else:
                logger.warning(f"Classify API returned {classify_resp.status_code}")
        except Exception as e:
            logger.warning(f"RAG promotion failed (non-fatal): {e}")

    # 标记已处理(v3.0: 附转写缓存 + 成本估算)
    # 成本为粗估: 中文约 2 字符/token,百炼混合费率按 ~0.002 USD/1K tokens 计
    tokens_est = (len(transcript.full_text) + len(visual_context)) // 2
    if visual_frames:
        tokens_est += len(visual_frames) * 800  # 每帧 VLM 调用粗估
    cost_est = round(tokens_est / 1000 * 0.002, 4)
    store.mark_processed(video_info.resolved_url, output_path, knowledge.title, video_id=video_id,
                         transcript=transcript.full_text, asr_engine=transcript.source,
                         tokens_used=tokens_est, cost_usd=cost_est)

    # ⭐ v3.0: 触发 RAG 增量入库(含 --vision 的 VLM 描述文本,图表信息可被检索)
    # 仅 A/B 级: C/D 级笔记 rag:false 会被 DKI 隔离(0 chunk),省去无效子进程
    if not getattr(args, 'no_ingest', False) and knowledge.content_tier in ("A", "B"):
        _trigger_rag_ingest(output_path)

    logger.info("=" * 50)
    logger.info(f"✅ Done! Output: {output_path}")
    print(f"✅ Done! Output: {output_path}")
    if getattr(args, 'json', False):
        print(json.dumps({"status": "ok", "output_path": str(output_path),
                          "title": knowledge.title, "tier": knowledge.content_tier,
                          "vision": bool(visual_frames)}, ensure_ascii=False))
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
    parser.add_argument("--vision", action="store_true",
                        help="视觉分析模式: 抽取关键帧送 VLM 提取图表/数据(隐含 --force,重跑时复用转写缓存)")
    parser.add_argument("--no-ingest", action="store_true", help="不触发 RAG 增量入库(调试用)")
    parser.add_argument("--output-dir", type=str, help="自定义输出目录")
    parser.add_argument("--cookies-from-browser", type=str, metavar="BROWSER",
                        help="从浏览器加载 cookies（chrome/safari/firefox/edge）")
    parser.add_argument("--cookies", type=str, metavar="FILE",
                        help="cookies 文件路径（Netscape 格式）")
    parser.add_argument("--debug", action="store_true", help="启用调试模式")
    parser.add_argument("--openclaw-payload", type=str, help="OpenClaw 消息 payload（JSON 或纯文本）")
    parser.add_argument("--json", action="store_true", help="JSON 格式输出结果")
    parser.add_argument("--instruction-file", type=str, metavar="PATH",
                        help="用户指令文件(代称映射/还原指令),并入蒸馏 prompt 最高优先级")

    args = parser.parse_args()

    # 配置日志级别
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    # --vision 隐含 --force(看完后二次提交的典型场景:已处理过,需重蒸馏)
    if getattr(args, 'vision', False):
        args.force = True

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