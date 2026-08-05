"""
入站消息日志
- 记录所有收到的飞书消息
- 支持审计、排查、统计
- Phase 1 of 飞书消息离线恢复方案

Created: 2026-03-18
"""

import sqlite3
from pathlib import Path
from datetime import datetime

# 数据库放在 zhiwei-dev 目录下（与 tasks.db 同目录）
DB_PATH = Path(__file__).parent.parent / "zhiwei-dev" / "message_log.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS message_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT UNIQUE NOT NULL,
    user_id TEXT NOT NULL,
    msg_type TEXT NOT NULL,
    content TEXT,
    received_at TEXT DEFAULT (datetime('now', 'localtime')),
    processed INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_received_at ON message_log(received_at);
CREATE INDEX IF NOT EXISTS idx_user_id ON message_log(user_id);
"""


class MessageLog:
    """入站消息日志管理器"""

    def __init__(self):
        self._init_db()

    def _init_db(self):
        """初始化数据库"""
        # 确保目录存在
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)

        with sqlite3.connect(DB_PATH) as conn:
            conn.executescript(SCHEMA)

    def log(self, message_id: str, user_id: str, msg_type: str, content: str = None) -> bool:
        """
        记录收到的消息

        Args:
            message_id: 飞书消息 ID
            user_id: 用户 ID
            msg_type: 消息类型 (text, image, audio, etc.)
            content: 消息内容（截断到 500 字符）

        Returns:
            bool: 是否成功写入（重复消息返回 False）
        """
        try:
            with sqlite3.connect(DB_PATH) as conn:
                # 截断内容
                truncated_content = content[:500] if content else None

                conn.execute(
                    "INSERT OR IGNORE INTO message_log (message_id, user_id, msg_type, content) VALUES (?, ?, ?, ?)",
                    (message_id, user_id, msg_type, truncated_content)
                )

                # 检查是否真的插入了（避免重复）
                return conn.total_changes > 0

        except Exception as e:
            print(f"⚠️ 消息日志写入失败: {e}")
            return False

    def mark_processed(self, message_id: str):
        """标记消息已处理"""
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    "UPDATE message_log SET processed = 1 WHERE message_id = ?",
                    (message_id,)
                )
        except Exception as e:
            print(f"⚠️ 标记消息处理状态失败: {e}")

    def get_unprocessed(self, min_age_seconds: int = 120, hours_limit: int = 6) -> list:
        """⭐ 2026-08-05: 获取未处理的文本消息（兜底补跑用）

        Args:
            min_age_seconds: 只取 N 秒前的消息（避免与正在处理的竞争）
            hours_limit: 只追溯 N 小时内的消息（太旧的不补）

        Returns:
            [{"message_id", "user_id", "content", "received_at"}, ...] 按时间正序
        """
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    """SELECT message_id, user_id, content, received_at FROM message_log
                       WHERE processed = 0 AND msg_type = 'text' AND content IS NOT NULL
                         AND received_at <= datetime('now', 'localtime', ?)
                         AND received_at >= datetime('now', 'localtime', ?)
                       ORDER BY id ASC LIMIT 10""",
                    (f"-{min_age_seconds} seconds", f"-{hours_limit} hours")
                )
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            print(f"⚠️ 查询未处理消息失败: {e}")
            return []

    def get_recent(self, limit: int = 100) -> list:
        """获取最近的消息记录"""
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT * FROM message_log ORDER BY id DESC LIMIT ?",
                (limit,)
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_stats(self) -> dict:
        """获取统计信息"""
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM message_log")
            total = cursor.fetchone()[0]

            cursor = conn.execute(
                "SELECT msg_type, COUNT(*) as cnt FROM message_log GROUP BY msg_type"
            )
            by_type = {row[0]: row[1] for row in cursor.fetchall()}

            return {
                "total": total,
                "by_type": by_type
            }

    def cleanup_old(self, days: int = 30):
        """清理旧消息记录（默认保留 30 天）"""
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.execute(
                "DELETE FROM message_log WHERE datetime(received_at) < datetime('now', ?)",
                (f"-{days} days",)
            )
            deleted = cursor.rowcount
            if deleted > 0:
                print(f"🗑️ 已清理 {deleted} 条旧消息记录")
            return deleted


# 全局实例
message_log = MessageLog()