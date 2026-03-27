"""
视频处理历史记录
用于检测重复视频链接，避免重复下载和处理

v2.0 新增：
- 错误类型和详细错误信息记录
- 重试次数跟踪
- 可重试失败记录查询
"""

import sqlite3
import hashlib
from pathlib import Path
from datetime import datetime
from contextlib import contextmanager
from enum import Enum
import logging

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "video_history.db"


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

# 需要发送告警的错误类型
ALERTABLE_ERRORS = [VideoErrorType.COOKIE_EXPIRED]

# 告警接收用户 ID（从环境变量或配置读取）
ALERT_USER_ID = None  # 在运行时设置

SCHEMA = """
CREATE TABLE IF NOT EXISTS video_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT UNIQUE NOT NULL,
    url_hash TEXT UNIQUE NOT NULL,
    title TEXT,
    status TEXT DEFAULT 'pending',
    output_path TEXT,
    processed_at TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    error_type TEXT,
    error_message TEXT,
    retry_count INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_url_hash ON video_history(url_hash);
CREATE INDEX IF NOT EXISTS idx_created_at ON video_history(created_at);
CREATE INDEX IF NOT EXISTS idx_status ON video_history(status);
"""


class VideoHistory:
    """视频处理历史管理器"""

    def __init__(self):
        self._init_db()
        self._migrate_schema()

    def _init_db(self):
        """初始化数据库"""
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.executescript(SCHEMA)
        logger.info(f"VideoHistory 初始化完成: {DB_PATH}")

    def _migrate_schema(self):
        """迁移旧表结构，添加新字段"""
        with sqlite3.connect(str(DB_PATH)) as conn:
            # 检查是否已有新字段
            cursor = conn.execute("PRAGMA table_info(video_history)")
            columns = [row[1] for row in cursor.fetchall()]

            if 'error_type' not in columns:
                conn.execute("ALTER TABLE video_history ADD COLUMN error_type TEXT")
                logger.info("Added column: error_type")

            if 'error_message' not in columns:
                conn.execute("ALTER TABLE video_history ADD COLUMN error_message TEXT")
                logger.info("Added column: error_message")

            if 'retry_count' not in columns:
                conn.execute("ALTER TABLE video_history ADD COLUMN retry_count INTEGER DEFAULT 0")
                logger.info("Added column: retry_count")

            # 添加状态索引（如果不存在）
            try:
                conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON video_history(status)")
            except sqlite3.OperationalError:
                pass

    @contextmanager
    def _connect(self):
        """数据库连接上下文管理器"""
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _hash_url(self, url: str) -> str:
        """标准化 URL 并计算 hash

        移除查询参数中的 tracking 参数，保留核心路径
        """
        # 移除查询参数中的 tracking 参数
        base_url = url.split('?')[0]
        return hashlib.sha256(base_url.encode()).hexdigest()[:16]

    def check_duplicate(self, url: str) -> dict | None:
        """检查 URL 是否已处理完成

        Returns:
            dict: 历史记录（包含 title, processed_at, output_path 等）
            None: 无记录
        """
        url_hash = self._hash_url(url)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM video_history WHERE url_hash = ? AND status = 'done'",
                (url_hash,)
            ).fetchone()
            if row:
                logger.info(f"检测到重复视频: {url[:50]}...")
            return dict(row) if row else None

    def record_start(self, url: str) -> int:
        """记录开始处理

        Returns:
            记录 ID
        """
        url_hash = self._hash_url(url)
        with self._connect() as conn:
            # 先尝试更新已有记录（支持重新处理）
            cursor = conn.execute("""
                INSERT INTO video_history (url, url_hash, status)
                VALUES (?, ?, 'processing')
                ON CONFLICT(url) DO UPDATE SET status = 'processing'
            """, (url, url_hash))
            logger.info(f"记录视频处理开始: {url[:50]}...")
            return cursor.lastrowid

    def record_done(self, url: str, title: str, output_path: str):
        """记录处理完成"""
        url_hash = self._hash_url(url)
        with self._connect() as conn:
            conn.execute("""
                UPDATE video_history SET status = 'done',
                    title = ?, output_path = ?,
                    processed_at = datetime('now', 'localtime')
                WHERE url_hash = ?
            """, (title, output_path, url_hash))
        logger.info(f"记录视频处理完成: {title}")

    def record_failed(self, url: str, error_type: str = None, error_message: str = None):
        """记录处理失败（增强版）

        Args:
            url: 视频 URL
            error_type: 错误类型（VideoErrorType.value）
            error_message: 详细错误信息
        """
        url_hash = self._hash_url(url)
        with self._connect() as conn:
            conn.execute("""
                UPDATE video_history SET status = 'failed',
                    error_type = ?, error_message = ?
                WHERE url_hash = ?
            """, (error_type, error_message, url_hash))
        logger.warning(f"记录视频处理失败: {url[:50]}... 错误类型: {error_type}")

    def increment_retry(self, url: str) -> int:
        """增加重试计数

        Returns:
            更新后的重试次数
        """
        url_hash = self._hash_url(url)
        with self._connect() as conn:
            conn.execute("""
                UPDATE video_history SET retry_count = retry_count + 1
                WHERE url_hash = ?
            """, (url_hash,))
            row = conn.execute(
                "SELECT retry_count FROM video_history WHERE url_hash = ?",
                (url_hash,)
            ).fetchone()
            return row['retry_count'] if row else 0

    def get_failed_for_retry(self, limit: int = 10) -> list[dict]:
        """获取可重试的失败记录

        筛选条件：
        - 状态为 failed
        - 错误类型在 RETRYABLE_ERRORS 中
        - 重试次数 < MAX_RETRIES

        Args:
            limit: 最大返回数量

        Returns:
            失败记录列表
        """
        retryable_types = [e.value for e in RETRYABLE_ERRORS]
        placeholders = ','.join('?' * len(retryable_types))

        with self._connect() as conn:
            rows = conn.execute(f"""
                SELECT * FROM video_history
                WHERE status = 'failed'
                  AND error_type IN ({placeholders})
                  AND retry_count < ?
                ORDER BY created_at ASC
                LIMIT ?
            """, (*retryable_types, MAX_RETRIES, limit)).fetchall()

            return [dict(r) for r in rows]

    def can_retry(self, url: str) -> bool:
        """检查是否可以重试

        Returns:
            True 如果可以重试
        """
        url_hash = self._hash_url(url)
        with self._connect() as conn:
            row = conn.execute("""
                SELECT error_type, retry_count FROM video_history
                WHERE url_hash = ?
            """, (url_hash,)).fetchone()

            if not row:
                return False

            error_type = row['error_type']
            retry_count = row['retry_count']

            if error_type in [e.value for e in RETRYABLE_ERRORS] and retry_count < MAX_RETRIES:
                return True
            return False

    def send_alert(self, error_type: VideoErrorType, url: str, message: str) -> bool:
        """发送飞书告警

        只有 ALERTABLE_ERRORS 中的错误类型才会发送告警。

        Args:
            error_type: 错误类型
            url: 视频 URL
            message: 错误信息

        Returns:
            是否发送成功
        """
        if error_type not in ALERTABLE_ERRORS:
            logger.debug(f"错误类型 {error_type.value} 不需要告警")
            return False

        if not ALERT_USER_ID:
            logger.warning("ALERT_USER_ID 未配置，跳过告警")
            return False

        try:
            # 延迟导入避免循环依赖
            try:
                from feishu_api import send_direct_message
            except ImportError:
                bot_dir = Path(__file__).parent
                if str(bot_dir) not in sys.path:
                    sys.path.insert(0, str(bot_dir))
                from feishu_api import send_direct_message

            error_type_display = {
                VideoErrorType.COOKIE_EXPIRED: "🍪 Cookie 过期",
                VideoErrorType.VIDEO_NOT_FOUND: "🗑️ 视频不存在",
                VideoErrorType.VIDEO_PRIVATE: "🔒 私密视频",
            }.get(error_type, f"❌ {error_type.value}")

            alert_msg = f"""⚠️ 抖音视频处理告警

{error_type_display}

URL: {url[:100]}...

详情: {message[:500]}

请检查并处理。"""

            success = send_direct_message(ALERT_USER_ID, alert_msg)
            if success:
                logger.info(f"告警已发送: {error_type.value}")
            else:
                logger.warning(f"告警发送失败")
            return success

        except Exception as e:
            logger.error(f"发送告警异常: {e}")
            return False

    def get_recent(self, limit: int = 10) -> list[dict]:
        """获取最近的处理记录"""
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT * FROM video_history
                ORDER BY created_at DESC LIMIT ?
            """, (limit,)).fetchall()
            return [dict(r) for r in rows]


# 全局实例
_history = None


def set_alert_user(user_id: str):
    """设置告警接收用户 ID

    Args:
        user_id: 飞书用户 open_id（以 ou_ 开头）
    """
    global ALERT_USER_ID
    ALERT_USER_ID = user_id
    logger.info(f"告警接收用户已设置: {user_id}")


def get_video_history() -> VideoHistory:
    """获取全局 VideoHistory 实例"""
    global _history
    if _history is None:
        _history = VideoHistory()
    return _history