"""
轻量链路追踪
- 生成 trace_id
- 结构化日志
"""
import uuid
import time
import json
import logging


def new_trace_id() -> str:
    """生成新的 trace_id"""
    return f"tr_{uuid.uuid4().hex[:12]}"


def log_structured(
    logger: logging.Logger,
    trace_id: str,
    module: str,
    action: str,
    **kwargs
) -> None:
    """
    输出结构化日志

    Args:
        logger: Python logger 实例
        trace_id: 链路追踪 ID
        module: 模块名（如 chat_handler, llm_client）
        action: 动作名（如 process, call, embed）
        **kwargs: 其他字段
    """
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "trace_id": trace_id,
        "module": module,
        "action": action,
        **kwargs
    }
    logger.info(json.dumps(entry, ensure_ascii=False))


class TraceContext:
    """链路追踪上下文管理器"""

    def __init__(self, module: str, action: str, logger: logging.Logger = None):
        self.module = module
        self.action = action
        self.trace_id = new_trace_id()
        self.logger = logger or logging.getLogger(__name__)
        self.start_time = None

    def __enter__(self):
        self.start_time = time.time()
        log_structured(
            self.logger, self.trace_id, self.module, f"{self.action}_start"
        )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        elapsed = time.time() - self.start_time
        status = "error" if exc_type else "success"
        log_structured(
            self.logger, self.trace_id, self.module, f"{self.action}_end",
            status=status,
            elapsed_ms=int(elapsed * 1000)
        )
        return False  # 不抑制异常