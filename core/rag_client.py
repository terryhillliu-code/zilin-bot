"""
RAG 统一调用入口（纯 HTTP 版）
- 收口所有 RAG 调用，统一走 HTTP API (http://127.0.0.1:8765)
- 2026-07-30: 废弃 subprocess bridge 降级路径（原 _search_via_bridge），
  HTTP 失败时返回明确错误（日志 + 空结果/error 字段），不再静默切换子进程
- 对上游只暴露 RAGClient / get_rag_client / rag_search
"""
import logging
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)

RAG_API_URL = "http://127.0.0.1:8765"


class RAGClientError(Exception):
    """RAG HTTP 调用失败（服务未启动/超时/非 200）"""


class RAGClient:
    """RAG 检索客户端（纯 HTTP）"""

    def __init__(self, api_url: str = RAG_API_URL):
        self.api_url = api_url

    # ---------- 内部 HTTP 封装 ----------

    def _post(self, path: str, payload: dict, timeout: int = 30) -> dict:
        """POST JSON；失败抛 RAGClientError（不静默）"""
        import requests
        url = f"{self.api_url}{path}"
        try:
            resp = requests.post(url, json=payload, timeout=timeout)
        except requests.exceptions.ConnectionError as e:
            raise RAGClientError(
                f"RAG 服务未启动或不可达 ({url}): {e}"
            ) from e
        except requests.exceptions.Timeout as e:
            raise RAGClientError(f"RAG 请求超时 ({url}, {timeout}s)") from e
        except Exception as e:
            raise RAGClientError(f"RAG 请求失败 ({url}): {e}") from e

        if resp.status_code != 200:
            raise RAGClientError(
                f"RAG API 错误 ({url}): HTTP {resp.status_code} {resp.text[:200]}"
            )
        return resp.json()

    def _get(self, path: str, timeout: int = 5) -> dict:
        """GET JSON；失败抛 RAGClientError"""
        import requests
        url = f"{self.api_url}{path}"
        try:
            resp = requests.get(url, timeout=timeout)
        except Exception as e:
            raise RAGClientError(f"RAG 请求失败 ({url}): {e}") from e
        if resp.status_code != 200:
            raise RAGClientError(f"RAG API 错误 ({url}): HTTP {resp.status_code}")
        return resp.json()

    # ---------- 检索 ----------

    def search(
        self,
        query: str,
        top_k: int = 5,
        source_filter: str = ""
    ) -> List[Dict[str, Any]]:
        """
        执行 RAG 检索（POST /search）

        Args:
            query: 查询文本
            top_k: 返回数量
            source_filter: 来源过滤（可选）

        Returns:
            [{"text": "...", "source": "...", "score": 0.85, ...}, ...]
            失败返回 []（错误已记录日志，调用方可据日志排查）
        """
        payload: Dict[str, Any] = {"query": query, "top_k": top_k}
        if source_filter:
            payload["source_filter"] = source_filter
        try:
            return self._post("/search", payload).get("results", [])
        except RAGClientError as e:
            logger.error(f"RAG 检索失败: {e}")
            return []

    def retrieve(
        self,
        query: str,
        top_k: int = 5,
        source_filter: str = ""
    ) -> List[Dict[str, Any]]:
        """完整检索（POST /retrieve，兼容 bridge.py 格式），失败返回 []"""
        payload: Dict[str, Any] = {"query": query, "top_k": top_k}
        if source_filter:
            payload["source_filter"] = source_filter
        try:
            return self._post("/retrieve", payload).get("results", [])
        except RAGClientError as e:
            logger.error(f"RAG retrieve 失败: {e}")
            return []

    def get_context(self, query: str, top_k: int = 5, timeout: int = 10) -> str:
        """
        获取检索上下文文本（供 rag_bridge.get_context 等场景使用）

        Returns:
            拼接后的上下文文本，失败返回空字符串（错误已记录日志）
        """
        payload = {"query": query, "top_k": top_k}
        try:
            results = self._post("/search", payload, timeout=timeout).get("results", [])
        except RAGClientError as e:
            logger.error(f"RAG get_context 失败: {e}")
            return ""

        parts = []
        for r in results[:top_k]:
            text = r.get("text", r.get("raw_text", ""))[:300]
            source = r.get("source", "")
            parts.append(f"【{source}】\n{text}" if source else text)
        return "\n\n".join(parts) if parts else ""

    # ---------- 分析 / 导入 / 图谱 ----------

    def analyze(self, query: str, top_k: int = 10, timeout: int = 120) -> Dict[str, Any]:
        """技术深度分析（POST /analyze），失败返回 {"error": "..."}"""
        try:
            return self._post("/analyze", {"query": query, "top_k": top_k}, timeout=timeout)
        except RAGClientError as e:
            logger.error(f"RAG analyze 失败: {e}")
            return {"error": str(e)}

    def import_article(
        self,
        title: str,
        content: str,
        source_type: str = "wechat",
        author: str = "",
        url: str = "",
        timeout: int = 120,
    ) -> Dict[str, Any]:
        """文章导入（POST /import，→Vault + papers.db + 观点提取），失败返回 {"status": "error", ...}"""
        payload = {
            "title": title,
            "content": content,
            "source_type": source_type,
            "author": author,
            "url": url,
        }
        try:
            return self._post("/import", payload, timeout=timeout)
        except RAGClientError as e:
            logger.error(f"RAG import 失败: {e}")
            return {"status": "error", "message": str(e)}

    def graph(self, timeout: int = 30, **kwargs) -> Dict[str, Any]:
        """
        知识图谱查询（POST /graph，7 种模式）

        kwargs 对应 GraphInput 字段：entity / paper_id / list_entities /
        layer / list_layers / from_layer / to_layer / evolution / ecosystem
        失败返回 {"error": "..."}
        """
        try:
            return self._post("/graph", kwargs, timeout=timeout)
        except RAGClientError as e:
            logger.error(f"RAG graph 失败: {e}")
            return {"error": str(e)}

    # ---------- 健康检查 ----------

    def is_available(self) -> bool:
        """RAG HTTP 服务是否可用（GET /health）"""
        try:
            self._get("/health", timeout=3)
            return True
        except RAGClientError:
            return False

    def health_check(self) -> dict:
        """
        检查 RAG 服务是否可用（保持返回格式兼容）

        Note: bridge_available 恒为 False —— subprocess bridge 路径已废弃，
        统一走 HTTP API。
        """
        result = {
            "api_available": False,
            "bridge_available": False,  # deprecated: 子进程路径已移除
            "doc_count": 0
        }
        try:
            data = self._get("/health", timeout=2)
            result["api_available"] = True
            result["doc_count"] = data.get("doc_count", 0)
        except RAGClientError as e:
            logger.warning(f"RAG health 检查失败: {e}")
        return result


# 单例
_client: Optional[RAGClient] = None


def get_rag_client() -> RAGClient:
    """获取 RAG 客户端单例"""
    global _client
    if _client is None:
        _client = RAGClient()
    return _client


def rag_search(query: str, top_k: int = 5, source_filter: str = "") -> List[Dict[str, Any]]:
    """便捷函数：执行 RAG 检索"""
    return get_rag_client().search(query, top_k, source_filter)
