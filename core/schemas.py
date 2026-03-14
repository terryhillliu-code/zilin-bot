"""
轻量任务协议
- TaskEnvelope: 任务封装
- ResultEnvelope: 结果封装
"""
from dataclasses import dataclass, field
from typing import Optional, List
import time

from .tracing import new_trace_id


@dataclass
class TaskEnvelope:
    """任务封装"""

    task_type: str           # chat/dev/rag/schedule/ingest
    source: str              # feishu/scheduler/cli
    payload: dict
    trace_id: str = field(default_factory=new_trace_id)
    created_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S%z"))

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "task_type": self.task_type,
            "source": self.source,
            "payload": self.payload,
            "trace_id": self.trace_id,
            "created_at": self.created_at
        }


@dataclass
class ResultEnvelope:
    """结果封装"""

    trace_id: str
    status: str              # success/failed/partial
    output: dict
    citations: List[dict] = field(default_factory=list)
    elapsed_ms: int = 0
    error: Optional[str] = None

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "trace_id": self.trace_id,
            "status": self.status,
            "output": self.output,
            "citations": self.citations,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error
        }

    @classmethod
    def success(cls, trace_id: str, output: dict, citations: List[dict] = None) -> 'ResultEnvelope':
        """创建成功结果"""
        return cls(
            trace_id=trace_id,
            status="success",
            output=output,
            citations=citations or []
        )

    @classmethod
    def failed(cls, trace_id: str, error: str, output: dict = None) -> 'ResultEnvelope':
        """创建失败结果"""
        return cls(
            trace_id=trace_id,
            status="failed",
            output=output or {},
            error=error
        )

    @classmethod
    def partial(cls, trace_id: str, output: dict, error: str, citations: List[dict] = None) -> 'ResultEnvelope':
        """创建部分成功结果"""
        return cls(
            trace_id=trace_id,
            status="partial",
            output=output,
            citations=citations or [],
            error=error
        )