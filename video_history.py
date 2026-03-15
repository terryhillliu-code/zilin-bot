"""
视频处理历史记录
用于检测重复视频链接，避免重复下载和处理
"""

import sqlite3
import hashlib
from pathlib import Path
from datetime import datetime
from contextlib import contextmanager
import logging

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "video_history.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS video_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT UNIQUE NOT NULL,
    url_hash TEXT UNIQUE NOT NULL,
    title TEXT,
    status TEXT DEFAULT 'pending',
    output_path TEXT,
    processed_at TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_url_hash ON video_history(url_hash);
CREATE INDEX IF NOT EXISTS idx_created_at ON video_history(created_at);
"""


class VideoHistory:
    """视频处理历史管理器"""

    def __init__(self):
        self._init_db()

    def _init_db(self):
        """初始化数据库"""
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.executescript(SCHEMA)
        logger.info(f"VideoHistory 初始化完成: {DB_PATH}")

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

    def record_failed(self, url: str):
        """记录处理失败"""
        url_hash = self._hash_url(url)
        with self._connect() as conn:
            conn.execute("""
                UPDATE video_history SET status = 'failed'
                WHERE url_hash = ?
            """, (url_hash,))
        logger.warning(f"记录视频处理失败: {url[:50]}...")

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


def get_video_history() -> VideoHistory:
    """获取全局 VideoHistory 实例"""
    global _history
    if _history is None:
        _history = VideoHistory()
    return _history