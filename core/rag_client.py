"""
RAG 统一调用入口
- 收口所有 RAG 调用
- 底层可以是 bridge / HTTP API
- 对上游只暴露一个接口
"""
import subprocess
import json
import logging
from typing import Optional, List, Dict, Any
from pathlib import Path

logger = logging.getLogger(__name__)


class RAGClient:
    """RAG 检索客户端"""

    def __init__(self):
        self.rag_dir = Path.home() / "zhiwei-rag"
        self.bridge_script = self.rag_dir / "bridge.py"
        self.venv_python = self.rag_dir / "venv/bin/python"
        self.api_url = "http://127.0.0.1:8765"
        self._use_api = None  # 延迟检测

    def _detect_api(self) -> bool:
        """检测是否使用 HTTP API"""
        import requests
        try:
            resp = requests.get(f"{self.api_url}/health", timeout=2)
            return resp.status_code == 200
        except Exception:
            return False

    def search(
        self,
        query: str,
        top_k: int = 5,
        source_filter: str = ""
    ) -> List[Dict[str, Any]]:
        """
        执行 RAG 检索

        Args:
            query: 查询文本
            top_k: 返回数量
            source_filter: 来源过滤（可选）

        Returns:
            [{"content": "...", "source": "...", "score": 0.85}, ...]
        """
        # 延迟检测 API
        if self._use_api is None:
            self._use_api = self._detect_api()

        if self._use_api:
            return self._search_via_api(query, top_k, source_filter)
        else:
            return self._search_via_bridge(query, top_k, source_filter)

    def _search_via_api(
        self,
        query: str,
        top_k: int,
        source_filter: str
    ) -> List[Dict[str, Any]]:
        """通过 HTTP API 检索"""
        import requests

        try:
            payload = {
                "query": query,
                "top_k": top_k
            }
            if source_filter:
                payload["source_filter"] = source_filter

            resp = requests.post(
                f"{self.api_url}/search",
                json=payload,
                timeout=30
            )

            if resp.status_code == 200:
                return resp.json().get("results", [])
            else:
                logger.warning(f"RAG API 错误: {resp.status_code}")
                return []

        except Exception as e:
            logger.error(f"RAG API 调用失败: {e}")
            return []

    def _search_via_bridge(
        self,
        query: str,
        top_k: int,
        source_filter: str
    ) -> List[Dict[str, Any]]:
        """通过 bridge 脚本检索"""
        if not self.bridge_script.exists():
            logger.warning(f"bridge.py 不存在: {self.bridge_script}")
            return []

        cmd = [
            str(self.venv_python),
            str(self.bridge_script),
            "retrieve", query,
            "--top-k", str(top_k),
        ]

        # Note: source_filter 暂不支持，bridge.py retrieve 命令无此参数

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,  # 增加超时，bridge 首次加载模型较慢
                cwd=str(self.rag_dir)
            )

            if result.returncode == 0:
                output = result.stdout.strip()
                if output:
                    return json.loads(output)
            else:
                logger.warning(f"bridge 错误: {result.stderr}")

            return []

        except subprocess.TimeoutExpired:
            logger.error("RAG bridge 超时")
            return []
        except json.JSONDecodeError as e:
            logger.error(f"RAG 结果解析失败: {e}")
            return []
        except Exception as e:
            logger.error(f"RAG 检索失败: {e}")
            return []

    def health_check(self) -> dict:
        """检查 RAG 服务是否可用"""
        result = {
            "api_available": False,
            "bridge_available": False,
            "doc_count": 0
        }

        # 检查 API
        import requests
        try:
            resp = requests.get(f"{self.api_url}/health", timeout=2)
            if resp.status_code == 200:
                result["api_available"] = True
                data = resp.json()
                result["doc_count"] = data.get("doc_count", 0)
        except Exception:
            pass

        # 检查 bridge + LanceDB
        try:
            check_result = subprocess.run(
                [
                    str(self.venv_python),
                    "-c",
                    "import lancedb; db=lancedb.connect('data/lance_db'); print(db.open_table('documents').count_rows())"
                ],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=str(self.rag_dir)
            )
            if check_result.returncode == 0:
                result["bridge_available"] = True
                if result["doc_count"] == 0:
                    result["doc_count"] = int(check_result.stdout.strip())
        except Exception:
            pass

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