#!/usr/bin/env python3
"""
语音任务存储模块
SQLite 存储语音识别提取的任务

数据表: voice_tasks
- id: 主键
- content: 任务内容
- source_text: 原始语音转文字
- status: pending/done/cancelled
- priority: high/normal/low
- created_at: 创建时间
- done_at: 完成时间
- note_path: Obsidian 笔记路径
"""

import os
import sqlite3
import json
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict

# 数据库路径
DB_PATH = os.path.expanduser("~/zhiwei-bot/data/voice_tasks.db")


class VoiceTaskStore:
    """语音任务存储类"""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or DB_PATH
        self._ensure_db()

    def _ensure_db(self):
        """确保数据库和表存在"""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS voice_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                source_text TEXT,
                status TEXT DEFAULT 'pending',
                priority TEXT DEFAULT 'normal',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                done_at TIMESTAMP,
                note_path TEXT
            )
        ''')

        # 创建索引加速查询
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_voice_tasks_status
            ON voice_tasks(status)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_voice_tasks_created_at
            ON voice_tasks(created_at)
        ''')

        conn.commit()
        conn.close()

    def add(self, content: str, priority: str = "normal",
            source_text: str = None) -> int:
        """添加新任务

        Args:
            content: 任务内容
            priority: 优先级 (high/normal/low)
            source_text: 原始语音转文字

        Returns:
            任务 ID
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute('''
            INSERT INTO voice_tasks (content, priority, source_text)
            VALUES (?, ?, ?)
        ''', (content, priority, source_text))

        task_id = cursor.lastrowid
        conn.commit()
        conn.close()

        return task_id

    def get(self, task_id: int) -> Optional[Dict]:
        """获取单个任务"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute('''
            SELECT id, content, source_text, status, priority,
                   created_at, done_at, note_path
            FROM voice_tasks WHERE id = ?
        ''', (task_id,))

        row = cursor.fetchone()
        conn.close()

        if row:
            return self._row_to_dict(row)
        return None

    def list_pending(self, limit: int = 20) -> List[Dict]:
        """获取待办任务列表"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute('''
            SELECT id, content, source_text, status, priority,
                   created_at, done_at, note_path
            FROM voice_tasks
            WHERE status = 'pending'
            ORDER BY
                CASE priority
                    WHEN 'high' THEN 1
                    WHEN 'normal' THEN 2
                    ELSE 3
                END,
                created_at ASC
            LIMIT ?
        ''', (limit,))

        rows = cursor.fetchall()
        conn.close()

        return [self._row_to_dict(row) for row in rows]

    def list_done_today(self, limit: int = 20) -> List[Dict]:
        """获取今日完成的任务"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        today = datetime.now().strftime('%Y-%m-%d')

        cursor.execute('''
            SELECT id, content, source_text, status, priority,
                   created_at, done_at, note_path
            FROM voice_tasks
            WHERE status = 'done'
              AND date(done_at) = ?
            ORDER BY done_at DESC
            LIMIT ?
        ''', (today, limit))

        rows = cursor.fetchall()
        conn.close()

        return [self._row_to_dict(row) for row in rows]

    def list_recent(self, limit: int = 20) -> List[Dict]:
        """获取最近的任务"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute('''
            SELECT id, content, source_text, status, priority,
                   created_at, done_at, note_path
            FROM voice_tasks
            ORDER BY created_at DESC
            LIMIT ?
        ''', (limit,))

        rows = cursor.fetchall()
        conn.close()

        return [self._row_to_dict(row) for row in rows]

    def mark_done(self, task_id: int, note_path: str = None) -> bool:
        """标记任务完成"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute('''
            UPDATE voice_tasks
            SET status = 'done',
                done_at = CURRENT_TIMESTAMP,
                note_path = ?
            WHERE id = ? AND status = 'pending'
        ''', (note_path, task_id))

        affected = cursor.rowcount
        conn.commit()
        conn.close()

        return affected > 0

    def cancel(self, task_id: int) -> bool:
        """取消任务"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute('''
            UPDATE voice_tasks
            SET status = 'cancelled'
            WHERE id = ? AND status = 'pending'
        ''', (task_id,))

        affected = cursor.rowcount
        conn.commit()
        conn.close()

        return affected > 0

    def stats(self) -> Dict:
        """获取任务统计"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute('''
            SELECT status, COUNT(*)
            FROM voice_tasks
            GROUP BY status
        ''')

        stats = {'pending': 0, 'done': 0, 'cancelled': 0}
        for row in cursor.fetchall():
            stats[row[0]] = row[1]

        # 今日完成数
        today = datetime.now().strftime('%Y-%m-%d')
        cursor.execute('''
            SELECT COUNT(*) FROM voice_tasks
            WHERE status = 'done' AND date(done_at) = ?
        ''', (today,))
        stats['done_today'] = cursor.fetchone()[0]

        conn.close()
        return stats

    def _row_to_dict(self, row) -> Dict:
        """将数据库行转换为字典"""
        return {
            'id': row[0],
            'content': row[1],
            'source_text': row[2],
            'status': row[3],
            'priority': row[4],
            'created_at': row[5],
            'done_at': row[6],
            'note_path': row[7]
        }


def create_daily_note(pending_tasks: List[Dict], done_tasks: List[Dict]) -> str:
    """创建每日任务 Obsidian 笔记

    Args:
        pending_tasks: 待办任务列表
        done_tasks: 今日完成任务列表

    Returns:
        笔记文件路径
    """
    vault_path = Path(os.path.expanduser("~/Documents/ZhiweiVault"))
    # 2026-07-31 修复: 原硬编码 "80-89_Work" 与 vault 规范目录 "80-89_工作文档_Work"
    # 分裂（目录改名后代码未跟上，mkdir 每晚重建旧名目录），已合并归一
    notes_dir = vault_path / "80-89_工作文档_Work" / "82_每日任务"
    notes_dir.mkdir(parents=True, exist_ok=True)

    today = datetime.now()
    filename = f"每日任务_{today.strftime('%Y-%m-%d')}.md"
    note_path = notes_dir / filename

    # 构建笔记内容
    lines = [
        f"# 每日任务 {today.strftime('%Y-%m-%d')}",
        "",
        "## 待办",
    ]

    if pending_tasks:
        for task in pending_tasks:
            priority_icon = "🔴" if task['priority'] == 'high' else \
                           "🟡" if task['priority'] == 'normal' else "⚪"
            lines.append(f"- [ ] {task['content']} ({priority_icon})")
    else:
        lines.append("暂无待办任务")

    lines.append("")
    lines.append("## 已完成")

    if done_tasks:
        for task in done_tasks:
            lines.append(f"- [x] {task['content']}")
    else:
        lines.append("暂无已完成任务")

    # 添加原始语音记录
    lines.append("")
    lines.append("## 原始语音记录")

    sources = set()
    for task in pending_tasks + done_tasks:
        if task.get('source_text'):
            sources.add(task['source_text'])

    for source in sources:
        lines.append(f"> {source}")

    # 写入文件
    with open(note_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))

    return str(note_path)


if __name__ == "__main__":
    # 测试
    store = VoiceTaskStore()
    print(f"📊 任务统计: {store.stats()}")

    # 添加测试任务
    # task_id = store.add("测试任务", "high", "这是一个测试语音")
    # print(f"✅ 添加任务 #{task_id}")