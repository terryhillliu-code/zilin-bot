"""会话上下文持久化存储 (2026-08-04 P1.1)

存储用户对话轮次(turns)与媒体产物(artifacts)元数据，供媒体追问意图
(media_followup)基于「最近处理的视频/文章」回答或带指令重析。替代
zhiwei_common.llm._session_store 的内存级会话(重启即失)与 ws_client
add_to_history 的截断展示历史。

回退开关：CONV_STORE=0 时所有方法退化为 no-op/空返回（建表/写入/查询前
统一判断 self._enabled）。建表失败亦自动降级为 no-op，绝不阻塞 bot 启动。

SQLite 红线遵守：WAL 模式 + 绝对路径，连接范式仿 zhiwei_common/task_store.py。
"""
import os
import sqlite3
import logging
from contextlib import contextmanager
from zhiwei_common.config import ZHIWEI_DEV

logger = logging.getLogger(__name__)

_DB_PATH = ZHIWEI_DEV / "conversation.db"

_KIND_ZH = {"video": "视频", "article": "文章", "podcast": "播客", "image": "图片"}
_ROLE_ZH = {"user": "用户", "assistant": "你", "system": "系统"}


class ConversationStore:
    def __init__(self, db_path=None):
        self._db_path = str(db_path) if db_path else str(_DB_PATH)
        # 回退开关：CONV_STORE=0 时全程 no-op（建表/写入/查询前判断）
        self._enabled = os.getenv("CONV_STORE", "1") != "0"
        if self._enabled:
            try:
                self._init_schema()
            except Exception as e:
                logger.warning(f"ConversationStore 建表失败，退化为 no-op: {e}")
                self._enabled = False

    def _init_schema(self):
        with self._connect() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS turns (
              id INTEGER PRIMARY KEY,
              user_id TEXT NOT NULL,
              role TEXT NOT NULL,
              content TEXT NOT NULL,
              kind TEXT DEFAULT 'chat',
              created_at TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS artifacts (
              id INTEGER PRIMARY KEY,
              user_id TEXT NOT NULL,
              kind TEXT NOT NULL,
              url TEXT,
              title TEXT,
              note_path TEXT,
              summary TEXT,
              instruction TEXT,
              created_at TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE INDEX IF NOT EXISTS idx_turns_user ON turns(user_id, id);
            CREATE INDEX IF NOT EXISTS idx_artifacts_user ON artifacts(user_id, id);
            """)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self._db_path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def record_turn(self, user_id, role, content, kind="chat"):
        """写入一轮对话；同事务内裁剪该 user 超出最近 50 条的旧 turns"""
        if not self._enabled:
            return
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO turns (user_id, role, content, kind) VALUES (?,?,?,?)",
                    (user_id, role, content, kind))
                conn.execute(
                    "DELETE FROM turns WHERE user_id=? AND id NOT IN "
                    "(SELECT id FROM turns WHERE user_id=? ORDER BY id DESC LIMIT 50)",
                    (user_id, user_id))
        except Exception as e:
            logger.warning(f"record_turn 失败: {e}")

    def register_artifact(self, user_id, kind, url=None, title=None, note_path=None, summary=None):
        """登记一个媒体产物，返回 artifact_id（int）；no-op 时返回 None"""
        if not self._enabled:
            return None
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "INSERT INTO artifacts (user_id, kind, url, title, note_path, summary) "
                    "VALUES (?,?,?,?,?,?)",
                    (user_id, kind, url, title, note_path, summary))
                return cur.lastrowid
        except Exception as e:
            logger.warning(f"register_artifact 失败: {e}")
            return None

    def get_last_artifact(self, user_id, kind=None):
        """返回最近一个产物(dict，含全部列)或 None"""
        if not self._enabled:
            return None
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                if kind:
                    row = conn.execute(
                        "SELECT * FROM artifacts WHERE user_id=? AND kind=? "
                        "ORDER BY id DESC LIMIT 1", (user_id, kind)).fetchone()
                else:
                    row = conn.execute(
                        "SELECT * FROM artifacts WHERE user_id=? "
                        "ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
                return dict(row) if row else None
        except Exception as e:
            logger.warning(f"get_last_artifact 失败: {e}")
            return None

    def build_context(self, user_id, max_chars=3000):
        """构造注入 LLM 的会话上下文字符串；无数据返回 ''"""
        if not self._enabled:
            return ""
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                artifact = conn.execute(
                    "SELECT * FROM artifacts WHERE user_id=? ORDER BY id DESC LIMIT 1",
                    (user_id,)).fetchone()
                turns = conn.execute(
                    "SELECT role, content FROM turns WHERE user_id=? "
                    "ORDER BY id DESC LIMIT 8", (user_id,)).fetchall()
            parts = []
            if artifact:
                a = dict(artifact)
                seg = f"【最近处理的内容】\n类型: {_KIND_ZH.get(a.get('kind'), '内容')}\n标题: {a.get('title') or '(无)'}"
                if a.get("summary"):
                    seg += f"\n摘要: {a['summary']}"
                if a.get("note_path"):
                    seg += f"\n笔记路径: {a['note_path']}"
                parts.append(seg)
            if turns:
                lines = ["【对话历史】"]
                for t in reversed(turns):  # DB 为 DESC，反转为正序展示
                    label = _ROLE_ZH.get(t["role"], t["role"])
                    lines.append(f"{label}: {t['content']}")
                parts.append("\n".join(lines))
            if not parts:
                return ""
            return "\n\n".join(parts)[:max_chars]
        except Exception as e:
            logger.warning(f"build_context 失败: {e}")
            return ""

    def set_instruction(self, artifact_id, instruction):
        """更新指定产物最近一次用户注入的指令(如代称映射)"""
        if not self._enabled:
            return
        try:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE artifacts SET instruction=? WHERE id=?",
                    (instruction, artifact_id))
        except Exception as e:
            logger.warning(f"set_instruction 失败: {e}")


# 模块级单例
conversation_store = ConversationStore()
