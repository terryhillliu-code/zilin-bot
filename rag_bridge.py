"""
zhiwei-rag 桥接（zhiwei-bot 版本）
"""
import os
import sys
import json
import subprocess
from pathlib import Path
from typing import Optional

RAG_DIR = Path.home() / "zhiwei-rag"
RAG_BRIDGE = RAG_DIR / "bridge.py"
RAG_VENV_PYTHON = RAG_DIR / "venv" / "bin" / "python"


def get_context(query: str, top_k: int = 5) -> str:
    """获取检索上下文"""
    if not RAG_BRIDGE.exists():
        return ""
    
    try:
        result = subprocess.run(
            [str(RAG_VENV_PYTHON), str(RAG_BRIDGE), "context", query, "--top-k", str(top_k)],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(RAG_DIR)
        )
        
        if result.returncode == 0:
            return result.stdout
        return ""
        
    except Exception as e:
        print(f"[RAG] 错误: {e}", file=sys.stderr)
        return ""


def is_available() -> bool:
    return RAG_BRIDGE.exists() and RAG_VENV_PYTHON.exists()
