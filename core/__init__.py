"""
知微核心模块
- llm_client: 统一 LLM 调用
- tracing: 链路追踪
- schemas: 任务协议
- rag_client: RAG 检索
- openclaw_adapter: OpenClaw 可选执行舱
"""
from .llm_client import LLMClient, LLMConfig, call_llm, call_llm_with_session
from .tracing import new_trace_id, log_structured, TraceContext
from .schemas import TaskEnvelope, ResultEnvelope
from .rag_client import RAGClient, get_rag_client, rag_search
from .openclaw_adapter import (
    OpenClawAdapter,
    openclaw_browser,
    openclaw_sandbox
)

__all__ = [
    # LLM
    "LLMClient",
    "LLMConfig",
    "call_llm",
    "call_llm_with_session",
    # Tracing
    "new_trace_id",
    "log_structured",
    "TraceContext",
    # Schemas
    "TaskEnvelope",
    "ResultEnvelope",
    # RAG
    "RAGClient",
    "get_rag_client",
    "rag_search",
    # OpenClaw
    "OpenClawAdapter",
    "openclaw_browser",
    "openclaw_sandbox",
]