"""模型路由封装模块

2026-07-31 解耦：原实现 sys.path.insert(~/zhiwei-dev) 后 import model_router，
依赖 **已于 7/25 退役** 的 zhiwei-dev（plist .DISABLED），属跨仓隐式耦合且
随时可能失效（ImportError 时静默降级为 None，路由能力悄悄消失）。
现将 model_router.get_best_model 的规则逻辑原样内联（纯字符串匹配，无外部依赖），
行为与原实现一致，不再跳仓。原始实现见 ~/zhiwei-dev/model_router.py（归档参考）。
"""
import logging

# 模型矩阵（源自 dev/model_router.py v33.0，OpenClaw 模型治理后）
MODELS = {
    "planner": "qwen3-max-2026-01-23",
    "coder_high": "qwen3-coder-plus",
    "coder_standard": "qwen3.5-plus",
    "long_context": "kimi-k2.5",       # 超长上下文专用
    "prompt_optimizer": "MiniMax-M2.5",
}

_HIGH_COMPLEXITY_KEYWORDS = [
    "重构", "架构", "底座", "解耦", "设计模式", "refactor", "architecture",
]
_CODING_KEYWORDS = [
    "实现", "开发", "编写", "bugfix", "fix", "implement", "coding",
]


def get_best_model(prompt_text: str, repo_path: str = None) -> str:
    """根据任务复杂度路由模型（规则同 dev/model_router.py）"""
    text_lower = (prompt_text or "").lower()

    # 1. 极长上下文或大规模审计 -> 长上下文模型
    if len(prompt_text or "") > 8000 or "审计" in text_lower or "全面分析" in text_lower:
        return MODELS["long_context"]

    # 2. 架构/重构等高复杂度 -> planner
    if any(kw in text_lower for kw in _HIGH_COMPLEXITY_KEYWORDS):
        return MODELS["planner"]

    # 3. 标准编码任务 -> coder_high
    if any(kw in text_lower for kw in _CODING_KEYWORDS):
        return MODELS["coder_high"]

    # 4. 简单任务/文档/单测 -> coder_standard
    return MODELS["coder_standard"]


def route_model_for_task(task_text: str) -> str | None:
    """根据任务文本路由到最佳模型"""
    try:
        return get_best_model(task_text)
    except Exception as e:
        logging.error(f"model routing failed: {e}")
        return None
