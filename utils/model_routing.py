"""模型路由封装模块"""
import sys
import os
import logging

# 一次性添加 zhiwei-dev 到 sys.path
ZHIWEI_DEV_PATH = os.path.expanduser("~/zhiwei-dev")
if ZHIWEI_DEV_PATH not in sys.path:
    sys.path.insert(0, ZHIWEI_DEV_PATH)

# 尝试导入，失败时提供 fallback
try:
    from model_router import get_best_model
except ImportError:
    logging.warning("model_router not found in zhiwei-dev")

    def get_best_model(_text: str) -> None:
        return None


def route_model_for_task(task_text: str) -> str | None:
    """根据任务文本路由到最佳模型"""
    try:
        return get_best_model(task_text)
    except Exception as e:
        logging.error(f"model_router failed: {e}")
        return None
