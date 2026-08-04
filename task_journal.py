# -*- coding: utf-8 -*-
"""任务账本（v70.6, 2026-08-03）：全类型蒸馏任务的生命周期留痕与断点恢复

背景：v70.5 只救了视频（video_history 有记录）；PDF/音频任务无任何留痕，
进程重启即蒸发。本模块为所有任务类型提供统一账本：
  开始(record_start) → 完成(record_done) / 失败(record_failed)
  → 看门狗扫描(get_stale) → 断点续跑(由 ws_client 调度)

文件类任务(pdf/audio)记录 message_id + file_key，中断后可从飞书重新下载，
不依赖本地 tmp 文件（处理完即删是既有设计，不受影响）。
"""

import logging
import os
import sqlite3
import threading

logger = logging.getLogger(__name__)

DB_PATH = os.path.expanduser("~/zhiwei-bot/data/task_journal.db")
MAX_RETRIES = 3
_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_type TEXT NOT NULL,          -- pdf | audio | （未来: url/image）
            message_id TEXT,                  -- 飞书消息 ID（重下载凭证）
            file_key TEXT,                    -- 飞书文件 key（重下载凭证）
            ref TEXT NOT NULL,                -- 文件名或 URL
            status TEXT NOT NULL DEFAULT 'processing',  -- processing|done|failed
            error_message TEXT,
            retry_count INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            processed_at TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tj_status ON task_journal(status, created_at)")
    return conn


def record_start(task_type: str, ref: str, message_id: str = "", file_key: str = "") -> int:
    """任务开始留痕，返回记录 ID"""
    with _lock, _connect() as conn:
        cur = conn.execute(
            "INSERT INTO task_journal (task_type, ref, message_id, file_key) VALUES (?, ?, ?, ?)",
            (task_type, ref, message_id, file_key))
        return cur.lastrowid


def record_done(record_id: int):
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE task_journal SET status='done', processed_at=datetime('now','localtime') WHERE id=?",
            (record_id,))


def record_failed(record_id: int, error: str):
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE task_journal SET status='failed', error_message=?, "
            "retry_count=retry_count+1, processed_at=datetime('now','localtime') WHERE id=?",
            (error[:300], record_id))


def get_stale(minutes: int = 30) -> list:
    """扫描疑似中断任务（processing 超时；进程被杀或工作线程静默死亡）"""
    with _lock, _connect() as conn:
        rows = conn.execute("""
            SELECT * FROM task_journal
            WHERE status='processing'
              AND created_at < datetime('now', 'localtime', ?)
            ORDER BY created_at ASC
        """, (f'-{minutes} minutes',)).fetchall()
        return [dict(r) for r in rows]


def mark_recovering(record_id: int):
    """补跑提交时标记 recovering：防看门狗重复捞起，失败时可回退"""
    with _lock, _connect() as conn:
        conn.execute("UPDATE task_journal SET status='recovering' WHERE id=?", (record_id,))


def mark_recovered(record_id: int):
    """补跑成功后标记 recovered（终态）"""
    with _lock, _connect() as conn:
        conn.execute("UPDATE task_journal SET status='recovered' WHERE id=?", (record_id,))


def mark_retry(record_id: int):
    """补跑失败回退为 processing 并累计重试次数（retry_count<3 才会被再捞）"""
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE task_journal SET status='processing', retry_count=retry_count+1 WHERE id=?",
            (record_id,))
