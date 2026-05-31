"""Claude 预处理器 — 汇总本地数据 + WebSearch，生成 NotebookLM 友好的摘要

用途: 当本地数据不足（<3 份文档）时，用 Claude 收集、汇总、清洗信息，
      输出结构化 Markdown 摘要，再上传到 NotebookLM 做深度分析。
"""
import os
import re
import json
import subprocess
import logging
from pathlib import Path
from .llm_client import llm_client

logger = logging.getLogger("claude_preprocessor")

# 本地知识库路径
OBSIDIAN_DIR = Path("/Users/liufang/Documents/ZhiweiVault/70-79_个人笔记/播客笔记")
PAPER_DB_SCRIPT = Path("/Users/liufang/arxiv-paper-analyzer/backend/scripts/manage.py")
PAPER_DB_PYTHON = Path("/Users/liufang/arxiv-paper-analyzer/backend/venv/bin/python3")


class ClaudePreprocessor:
    def __init__(self, export_root: Path):
        self.export_root = export_root

    def _gather_obsidian_notes(self, topic: str, limit: int = 5) -> str:
        """从 Obsidian 播客笔记中检索相关内容"""
        if not OBSIDIAN_DIR.exists():
            return "（无 Obsidian 笔记）"

        results = []
        topic_lower = topic.lower()

        for md_file in OBSIDIAN_DIR.glob("*.md"):
            try:
                content = md_file.read_text(encoding="utf-8")
                # 简单关键词匹配
                if any(kw.lower() in content.lower() for kw in topic_lower.split()):
                    # 提取标题和前 2000 字
                    title_match = re.search(r"title:\s*(.+)", content)
                    title = title_match.group(1).strip() if title_match else md_file.stem
                    preview = content[:2000]
                    results.append(f"### [{title}] ({md_file.name})\n{preview}")
                    if len(results) >= limit:
                        break
            except Exception as e:
                logger.warning(f"读取笔记失败 {md_file}: {e}")

        if not results:
            return "（未找到匹配的 Obsidian 笔记）"

        return "\n\n---\n\n".join(results)

    def _gather_papers(self, topic: str, limit: int = 5) -> str:
        """从论文数据库中检索相关内容"""
        if not PAPER_DB_SCRIPT.exists():
            return "（无论文数据库）"

        try:
            cmd = [
                str(PAPER_DB_PYTHON), str(PAPER_DB_SCRIPT),
                "export-notebook",
                "--query", topic,
                "--limit", str(limit),
                "--tiers", "A,B",
                "--dry-run",  # 只查询不导出
            ]
            result = subprocess.run(
                cmd,
                cwd=str(PAPER_DB_SCRIPT.parent.parent),
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout[:3000]
            return "（未找到匹配的论文）"
        except Exception as e:
            logger.warning(f"论文检索失败: {e}")
            return "（论文检索失败）"

    def _web_search(self, topic: str) -> str:
        """调用 WebSearch 获取最新信息（通过 zhiwei-rag）"""
        try:
            cmd = [
                "/Users/liufang/zhiwei-shared-venv/bin/python3",
                "-c",
                f"""
from zhiwei_rag.web_search import web_search
results = web_search("{topic}", count=5)
import json
print(json.dumps(results, ensure_ascii=False)[:3000])
"""
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0 and result.stdout:
                return result.stdout
            return "（网络搜索失败或无结果）"
        except Exception as e:
            logger.warning(f"网络搜索失败: {e}")
            return "（网络搜索不可用）"

    def preprocess(self, topic: str) -> str:
        """完整预处理流程：收集 → Claude 汇总 → 保存"""
        logger.info(f"开始预处理主题: {topic}")

        # Step 1: 收集本地数据
        obsidian_notes = self._gather_obsidian_notes(topic)
        papers = self._gather_papers(topic)

        # Step 2: WebSearch 补充
        search_results = self._web_search(topic)

        # Step 3: Claude 汇总
        prompt = f"""你是知微系统的首席技术分析师。请基于以下材料，生成一份 NotebookLM 分析摘要。

【主题】{topic}

【本地数据摘要】
{obsidian_notes}

【论文数据库检索结果】
{papers}

【网络搜索结果】
{search_results}

【输出要求】
1. 执行摘要（300 字）
2. 核心事实与数据（按主题分章节，每章 400 字+）
3. 矛盾/待验证项（列出 conflicting claims）
4. 关键术语表
5. 每个论断标注来源（[本地笔记] 或 [论文] 或 [网络] + URL）
6. 总输出 2000-3000 字，中文输出

注意：
- 如果某类数据缺失（如无本地笔记），明确标注"本节基于网络搜索"
- 不要编造数据，没有的信息直接说"暂无数据"
- 输出纯 Markdown 格式"""

        success, result = llm_client.call(
            role="research",
            message=prompt,
            system_prompt=(
                "你是知微系统的首席技术分析师，负责将碎片化的技术信息整理为"
                "结构化的分析摘要，供 NotebookLM 进行深度交叉引用分析。"
            ),
            timeout=180
        )

        if not success:
            raise RuntimeError(f"Claude 汇总失败: {result}")

        # Step 4: 保存到 export_root
        safe_topic = re.sub(r'[^\w一-鿿]', '_', topic[:20])
        output_path = self.export_root / f"preprocessed_{safe_topic}.md"
        output_path.write_text(result, encoding="utf-8")

        logger.info(f"预处理完成: {output_path}")
        return str(output_path)
