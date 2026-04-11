"""
zhiwei-rag 桥接（zhiwei-bot 版本）

通过 HTTP API 调用 RAG 服务，替代子进程隔离方案
"""
import json
import sys
import urllib.request
import urllib.error
from typing import Optional

RAG_API_URL = "http://127.0.0.1:8765"
TIMEOUT = 10


def _post_json(url: str, data: dict, timeout: int = TIMEOUT) -> Optional[dict]:
    """发送 POST JSON 请求并返回解析结果"""
    try:
        body = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[RAG] HTTP 请求失败: {e}", file=sys.stderr)
        return None


def get_context(query: str, top_k: int = 5, timeout: int = 10) -> str:
    """
    获取检索上下文

    Args:
        query: 查询文本
        top_k: 返回结果数量
        timeout: 超时时间（秒）

    Returns:
        检索到的上下文文本，失败返回空字符串
    """
    result = _post_json(
        f"{RAG_API_URL}/search",
        {"query": query, "top_k": top_k},
        timeout=timeout,
    )

    if not result or "results" not in result:
        return ""

    parts = []
    for r in result["results"][:top_k]:
        text = r.get("text", r.get("raw_text", ""))[:300]
        source = r.get("source", "")
        if source:
            parts.append(f"【{source}】\n{text}")
        else:
            parts.append(text)

    return "\n\n".join(parts) if parts else ""


def is_available() -> bool:
    """检查 RAG 服务是否可用"""
    try:
        req = urllib.request.Request(f"{RAG_API_URL}/health")
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False
