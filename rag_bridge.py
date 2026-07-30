"""
[DEPRECATED] zhiwei-rag 桥接（zhiwei-bot 版本）

⚠️ 本模块已废弃（2026-07-30，Task 8：统一 RAG 调用方式）。
请改用 core/rag_client.py 的 HTTP 实现：

    from core.rag_client import get_rag_client, rag_search
    client = get_rag_client()
    client.get_context(query, top_k=5)   # 等价于本模块 get_context
    client.is_available()                # 等价于本模块 is_available

本模块保留仅为兼容存量调用方（ws_client.py / commands/chat_handler.py /
scripts/test_commands.py），内部已全部转发到 core.rag_client 的 HTTP 实现，
函数签名与返回格式保持不变。
"""
import sys
import warnings

warnings.warn(
    "rag_bridge 已废弃，请改用 core.rag_client (HTTP)；"
    "本模块仅作转发兼容层保留",
    DeprecationWarning,
    stacklevel=2,
)

RAG_API_URL = "http://127.0.0.1:8765"
TIMEOUT = 10


def get_context(query: str, top_k: int = 5, timeout: int = 10) -> str:
    """
    [DEPRECATED] 获取检索上下文 → 转发到 core.rag_client.RAGClient.get_context

    Args:
        query: 查询文本
        top_k: 返回结果数量
        timeout: 超时时间（秒）

    Returns:
        检索到的上下文文本，失败返回空字符串
    """
    try:
        from core.rag_client import get_rag_client
        return get_rag_client().get_context(query, top_k=top_k, timeout=timeout)
    except Exception as e:
        print(f"[RAG] HTTP 请求失败: {e}", file=sys.stderr)
        return ""


def is_available() -> bool:
    """[DEPRECATED] 检查 RAG 服务是否可用 → 转发到 core.rag_client"""
    try:
        from core.rag_client import get_rag_client
        return get_rag_client().is_available()
    except Exception:
        return False
