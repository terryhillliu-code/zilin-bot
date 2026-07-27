#!/usr/bin/env python3
"""
视频处理缓存层 - SQLite实现
功能：URL去重、转录缓存、成本记录
零依赖额外安装，仅使用Python标准库
"""

import sqlite3
import hashlib
import json
import time
from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime


# 默认缓存数据库路径
DEFAULT_DB_PATH = Path(__file__).parent.parent / "data" / "video_cache.db"


def get_db_connection(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """获取数据库连接，自动建表"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # 提高并发性能
    conn.execute("PRAGMA synchronous=NORMAL")
    _init_tables(conn)
    return conn


def _init_tables(conn: sqlite3.Connection):
    """初始化表结构，支持增量升级已有数据库"""
    # 检查video_records表是否有archive_path字段，没有则升级
    cursor = conn.execute("PRAGMA table_info(video_records)")
    columns = [row[1] for row in cursor.fetchall()]
    if "archive_path" not in columns:
        # 新增归档相关字段
        conn.execute("ALTER TABLE video_records ADD COLUMN archive_path TEXT")
        conn.execute("ALTER TABLE video_records ADD COLUMN audio_path TEXT")
        conn.execute("ALTER TABLE video_records ADD COLUMN transcript_path TEXT")
        conn.execute("ALTER TABLE video_records ADD COLUMN knowledge_path TEXT")
        conn.execute("ALTER TABLE video_records ADD COLUMN archived_at TIMESTAMP")
        conn.commit()

    conn.executescript("""
        -- 视频处理主表：去重 + 处理状态
        CREATE TABLE IF NOT EXISTS video_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url_hash TEXT UNIQUE NOT NULL,
            url TEXT NOT NULL,
            platform TEXT NOT NULL,  -- douyin/bilibili/youtube/other
            video_id TEXT,
            title TEXT,
            author TEXT,
            duration INTEGER,  -- 秒
            status TEXT NOT NULL DEFAULT 'pending',  -- pending/processing/success/fail
            retry_count INTEGER DEFAULT 0,
            error_message TEXT,
            report_path TEXT,  -- 生成的Markdown报告路径
            -- 归档相关字段
            archive_path TEXT,  -- 归档目录路径
            audio_path TEXT,    -- 归档的音频路径
            transcript_path TEXT, -- 转录原文路径
            knowledge_path TEXT, -- 知识图谱路径
            archived_at TIMESTAMP, -- 归档时间
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- 转录缓存表：避免重复转录
        CREATE TABLE IF NOT EXISTS transcript_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url_hash TEXT UNIQUE NOT NULL,
            url TEXT NOT NULL,
            transcript_json TEXT NOT NULL,  -- 转录结果JSON
            language TEXT,
            segment_count INTEGER,
            word_count INTEGER,
            asr_engine TEXT,  -- mlx_whisper/openai/dashscope
            asr_duration REAL,  -- 转录耗时秒
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- 成本记录表：统计LLM调用成本
        CREATE TABLE IF NOT EXISTS cost_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url_hash TEXT NOT NULL,
            url TEXT NOT NULL,
            call_type TEXT NOT NULL,  -- chunk_analysis/summary/other
            model TEXT NOT NULL,
            input_tokens INTEGER,
            output_tokens INTEGER,
            total_tokens INTEGER,
            cost_usd REAL,
            latency_ms INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- 知识图谱表：存储技术术语和关联关系
        CREATE TABLE IF NOT EXISTS knowledge_graph (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url_hash TEXT NOT NULL,
            url TEXT NOT NULL,
            term TEXT NOT NULL,                   -- 技术术语
            definition TEXT,                      -- 定义
            related_terms TEXT,                   -- 关联术语JSON列表
            related_papers TEXT,                  -- 相关论文JSON列表
            related_projects TEXT,                -- 相关开源项目JSON列表
            learning_resources TEXT,              -- 学习资源JSON列表
            graph_position TEXT,                  -- 在知识图谱中的位置JSON
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- 深度观点表
        CREATE TABLE IF NOT EXISTS deep_viewpoints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url_hash TEXT NOT NULL,
            url TEXT NOT NULL,
            viewpoint TEXT NOT NULL,              -- 核心观点
            support_logic TEXT,                   -- 支撑逻辑
            applicability TEXT,                   -- 适用范围
            limitations TEXT,                     -- 局限性
            related_topics TEXT,                  -- 关联话题JSON列表
            confidence REAL,                      -- 置信度0-1
            timestamp TEXT,                       -- 视频中的时间点
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- 索引优化
        CREATE INDEX IF NOT EXISTS idx_video_records_url_hash ON video_records(url_hash);
        CREATE INDEX IF NOT EXISTS idx_video_records_status ON video_records(status);
        CREATE INDEX IF NOT EXISTS idx_transcript_cache_url_hash ON transcript_cache(url_hash);
        CREATE INDEX IF NOT EXISTS idx_cost_records_url_hash ON cost_records(url_hash);
        CREATE INDEX IF NOT EXISTS idx_cost_records_created_at ON cost_records(created_at);
        CREATE INDEX IF NOT EXISTS idx_knowledge_graph_url_hash ON knowledge_graph(url_hash);
        CREATE INDEX IF NOT EXISTS idx_knowledge_graph_term ON knowledge_graph(term);
        CREATE INDEX IF NOT EXISTS idx_deep_viewpoints_url_hash ON deep_viewpoints(url_hash);
    """)
    conn.commit()


def hash_url(url: str) -> str:
    """生成URL的唯一hash"""
    return hashlib.sha256(url.strip().encode('utf-8')).hexdigest()


class VideoCache:
    """视频处理缓存操作类"""

    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self.db_path = db_path
        self.conn = get_db_connection(db_path)

    def close(self):
        """关闭数据库连接"""
        if self.conn:
            self.conn.close()
            self.conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    # ==================== URL去重 ==================

    def is_url_processed(self, url: str) -> bool:
        """检查URL是否已成功处理过"""
        url_hash = hash_url(url)
        row = self.conn.execute(
            "SELECT id FROM video_records WHERE url_hash = ? AND status = 'success'",
            (url_hash,)
        ).fetchone()
        return row is not None

    def get_processed_result(self, url: str) -> Optional[Dict[str, Any]]:
        """获取已处理URL的结果（报告路径等）"""
        url_hash = hash_url(url)
        row = self.conn.execute(
            "SELECT * FROM video_records WHERE url_hash = ? AND status = 'success'",
            (url_hash,)
        ).fetchone()
        if row:
            return dict(row)
        return None

    def create_record(self, url: str, platform: str, video_id: str = None,
                      title: str = None, author: str = None, duration: int = None) -> str:
        """创建处理记录"""
        url_hash = hash_url(url)
        try:
            self.conn.execute("""
                INSERT OR IGNORE INTO video_records
                (url_hash, url, platform, video_id, title, author, duration, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
            """, (url_hash, url, platform, video_id, title, author, duration))
            self.conn.commit()
            return url_hash
        except sqlite3.IntegrityError:
            # 已存在，返回现有hash
            return url_hash

    def update_status(self, url_hash: str, status: str, report_path: str = None,
                      error_message: str = None, retry_count: int = None,
                      archive_path: str = None, audio_path: str = None,
                      transcript_path: str = None, knowledge_path: str = None):
        """更新处理状态，支持归档路径等新增字段"""
        updates = ["status = ?", "updated_at = CURRENT_TIMESTAMP"]
        params = [status]
        if report_path:
            updates.append("report_path = ?")
            params.append(report_path)
        if error_message:
            updates.append("error_message = ?")
            params.append(error_message)
        if retry_count is not None:
            updates.append("retry_count = ?")
            params.append(retry_count)
        if archive_path:
            updates.append("archive_path = ?")
            params.append(archive_path)
        if audio_path:
            updates.append("audio_path = ?")
            params.append(audio_path)
        if transcript_path:
            updates.append("transcript_path = ?")
            params.append(transcript_path)
        if knowledge_path:
            updates.append("knowledge_path = ?")
            params.append(knowledge_path)
            updates.append("archived_at = CURRENT_TIMESTAMP")
        params.append(url_hash)
        self.conn.execute(
            f"UPDATE video_records SET {', '.join(updates)} WHERE url_hash = ?",
            params
        )
        self.conn.commit()

    def get_failed_records(self, limit: int = 5, max_retries: int = 3) -> list:
        """获取可重试的失败记录"""
        rows = self.conn.execute("""
            SELECT * FROM video_records
            WHERE status = 'fail' AND retry_count < ?
            ORDER BY updated_at ASC
            LIMIT ?
        """, (max_retries, limit)).fetchall()
        return [dict(row) for row in rows]

    # ==================== 转录缓存 ==================

    def get_transcript(self, url: str) -> Optional[Dict[str, Any]]:
        """获取缓存的转录结果"""
        url_hash = hash_url(url)
        row = self.conn.execute(
            "SELECT * FROM transcript_cache WHERE url_hash = ?",
            (url_hash,)
        ).fetchone()
        if row:
            result = dict(row)
            result['transcript_json'] = json.loads(result['transcript_json'])
            return result
        return None

    def save_transcript(self, url: str, transcript: list, language: str,
                        asr_engine: str, asr_duration: float) -> str:
        """保存转录结果到缓存"""
        url_hash = hash_url(url)
        word_count = sum(len(seg.get('text', '')) for seg in transcript)
        self.conn.execute("""
            INSERT OR REPLACE INTO transcript_cache
            (url_hash, url, transcript_json, language, segment_count, word_count, asr_engine, asr_duration)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            url_hash, url,
            json.dumps(transcript, ensure_ascii=False),
            language,
            len(transcript),
            word_count,
            asr_engine,
            asr_duration
        ))
        self.conn.commit()
        return url_hash

    # ==================== 成本记录 ==================

    def record_cost(self, url: str, call_type: str, model: str,
                    input_tokens: int, output_tokens: int, latency_ms: int):
        """记录单次LLM调用成本"""
        url_hash = hash_url(url)
        total_tokens = input_tokens + output_tokens
        # 通义千问qwen3.7-plus定价：输入0.001元/1k tokens，输出0.003元/1k tokens
        cost_usd = (input_tokens / 1000 * 0.001 + output_tokens / 1000 * 0.003) / 7.2  # 换算美元
        self.conn.execute("""
            INSERT INTO cost_records
            (url_hash, url, call_type, model, input_tokens, output_tokens, total_tokens, cost_usd, latency_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (url_hash, url, call_type, model, input_tokens, output_tokens, total_tokens, cost_usd, latency_ms))
        self.conn.commit()

    def get_cost_summary(self, days: int = 7) -> Dict[str, Any]:
        """获取最近N天的成本汇总"""
        row = self.conn.execute("""
            SELECT
                COUNT(*) as total_calls,
                SUM(total_tokens) as total_tokens,
                SUM(cost_usd) as total_cost_usd,
                AVG(latency_ms) as avg_latency_ms,
                COUNT(DISTINCT url_hash) as unique_videos
            FROM cost_records
            WHERE created_at >= datetime('now', ?)
        """, (f'-{days} days',)).fetchone()
        return dict(row) if row else {}

    def get_cost_by_model(self, days: int = 7) -> list:
        """按模型汇总成本"""
        rows = self.conn.execute("""
            SELECT model,
                COUNT(*) as calls,
                SUM(total_tokens) as tokens,
                SUM(cost_usd) as cost_usd
            FROM cost_records
            WHERE created_at >= datetime('now', ?)
            GROUP BY model
            ORDER BY cost_usd DESC
        """, (f'-{days} days',)).fetchall()
        return [dict(row) for row in rows]

    # ==================== 知识图谱操作 ====================

    def save_knowledge_term(self, url: str, term: str, definition: str,
                           related_terms: list = None, related_papers: list = None,
                           related_projects: list = None, learning_resources: list = None,
                           graph_position: dict = None) -> int:
        """保存技术术语到知识图谱"""
        url_hash = hash_url(url)
        cursor = self.conn.execute("""
            INSERT INTO knowledge_graph
            (url_hash, url, term, definition, related_terms, related_papers, related_projects, learning_resources, graph_position)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            url_hash, url, term, definition,
            json.dumps(related_terms or [], ensure_ascii=False),
            json.dumps(related_papers or [], ensure_ascii=False),
            json.dumps(related_projects or [], ensure_ascii=False),
            json.dumps(learning_resources or [], ensure_ascii=False),
            json.dumps(graph_position or {}, ensure_ascii=False)
        ))
        self.conn.commit()
        return cursor.lastrowid

    def save_deep_viewpoint(self, url: str, viewpoint: str, support_logic: str,
                           applicability: str, limitations: str, related_topics: list = None,
                           confidence: float = 0.5, timestamp: str = None) -> int:
        """保存深度观点"""
        url_hash = hash_url(url)
        cursor = self.conn.execute("""
            INSERT INTO deep_viewpoints
            (url_hash, url, viewpoint, support_logic, applicability, limitations, related_topics, confidence, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            url_hash, url, viewpoint, support_logic, applicability, limitations,
            json.dumps(related_topics or [], ensure_ascii=False),
            confidence, timestamp
        ))
        self.conn.commit()
        return cursor.lastrowid

    def get_knowledge_by_term(self, term: str) -> Optional[Dict[str, Any]]:
        """按技术术语查询知识图谱（跨视频聚合）"""
        row = self.conn.execute("""
            SELECT * FROM knowledge_graph
            WHERE term = ?
            ORDER BY created_at DESC
            LIMIT 1
        """, (term,)).fetchone()
        if row:
            result = dict(row)
            # 解析JSON字段
            for field in ['related_terms', 'related_papers', 'related_projects', 'learning_resources', 'graph_position']:
                try:
                    result[field] = json.loads(result[field])
                except (json.JSONDecodeError, TypeError):
                    result[field] = [] if field != 'graph_position' else {}
            return result
        return None

    def get_viewpoints_by_url(self, url: str) -> list:
        """获取视频的所有深度观点"""
        url_hash = hash_url(url)
        rows = self.conn.execute("""
            SELECT * FROM deep_viewpoints
            WHERE url_hash = ?
            ORDER BY timestamp ASC
        """, (url_hash,)).fetchall()
        results = []
        for row in rows:
            result = dict(row)
            try:
                result['related_topics'] = json.loads(result['related_topics'])
            except (json.JSONDecodeError, TypeError):
                result['related_topics'] = []
            results.append(result)
        return results

    def get_related_terms(self, term: str) -> list:
        """获取与指定术语关联的所有术语"""
        rows = self.conn.execute("""
            SELECT term, related_terms FROM knowledge_graph
            WHERE term = ?
        """, (term,)).fetchall()
        related = set()
        for row in rows:
            try:
                terms = json.loads(row['related_terms'])
                related.update(terms)
            except (json.JSONDecodeError, TypeError):
                pass
        return list(related)


if __name__ == "__main__":
    # 测试代码
    print("=== 视频缓存模块测试 ===")
    with VideoCache() as cache:
        # 测试URL去重
        test_url = "https://www.bilibili.com/video/BV1Zb411v7ak"
        cache.create_record(test_url, "bilibili", "BV1Zb411v7ak", "测试视频", "测试作者", 300)
        print(f"URL是否已处理: {cache.is_url_processed(test_url)}")

        # 测试转录缓存
        fake_transcript = [{"start": 0, "end": 5, "text": "这是一段测试转录"}]
        cache.save_transcript(test_url, fake_transcript, "zh", "mlx_whisper", 2.5)
        cached = cache.get_transcript(test_url)
        print(f"转录缓存命中: {cached is not None}")

        # 测试成本记录
        cache.record_cost(test_url, "chunk_analysis", "qwen3.7-plus", 1500, 800, 3200)
        summary = cache.get_cost_summary()
        print(f"成本汇总: {summary}")

        # 测试知识图谱存储
        cache.save_knowledge_term(
            url=test_url,
            term="Transformer",
            definition="一种基于自注意力机制的深度学习架构",
            related_terms=["Self-Attention", "BERT", "GPT"],
            related_papers=["Attention Is All You Need"],
            related_projects=["Hugging Face Transformers"]
        )
        term = cache.get_knowledge_by_term("Transformer")
        print(f"知识图谱存储: {term is not None}")

        # 测试深度观点存储
        cache.save_deep_viewpoint(
            url=test_url,
            viewpoint="Transformer突破了RNN的序列建模限制",
            support_logic="自注意力机制可以并行计算全局依赖",
            applicability="自然语言处理、计算机视觉、语音识别",
            limitations="长序列场景下计算复杂度高",
            confidence=0.92,
            timestamp="00:02:30"
        )
        viewpoints = cache.get_viewpoints_by_url(test_url)
        print(f"深度观点数量: {len(viewpoints)}")

    print("✅ 测试通过")
