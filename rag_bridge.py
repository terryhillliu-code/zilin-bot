"""
zhiwei-rag 桥接（zhiwei-bot 版本）

提供知识库检索能力，通过子进程隔离依赖环境
"""
import os
import sys
import subprocess
from pathlib import Path
from typing import Optional

RAG_DIR = Path.home() / "zhiwei-rag"
RAG_BRIDGE = RAG_DIR / "bridge.py"
RAG_VENV_PYTHON = RAG_DIR / "venv" / "bin" / "python3"


def get_context(query: str, top_k: int = 5, timeout: int = 40) -> str:
    """
    获取检索上下文

    Args:
        query: 查询文本
        top_k: 返回结果数量
        timeout: 超时时间（秒）

    Returns:
        检索到的上下文文本，失败返回空字符串
    """
    if not RAG_BRIDGE.exists():
        print(f"[RAG] bridge.py 不存在: {RAG_BRIDGE}", file=sys.stderr)
        return ""

    if not RAG_VENV_PYTHON.exists():
        print(f"[RAG] venv python 不存在: {RAG_VENV_PYTHON}", file=sys.stderr)
        return ""

    try:
        result = subprocess.run(
            [str(RAG_VENV_PYTHON), str(RAG_BRIDGE), "context", query, "--top-k", str(top_k)],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(RAG_DIR)
        )

        if result.returncode == 0:
            return result.stdout.strip()
        else:
            print(f"[RAG] 检索失败: {result.stderr[:200]}", file=sys.stderr)
            return ""

    except subprocess.TimeoutExpired:
        print(f"[RAG] 检索超时 ({timeout}s)", file=sys.stderr)
        return ""
    except Exception as e:
        print(f"[RAG] 错误: {e}", file=sys.stderr)
        return ""


def is_available() -> bool:
    """检查 RAG 服务是否可用"""
    return RAG_BRIDGE.exists() and RAG_VENV_PYTHON.exists()
