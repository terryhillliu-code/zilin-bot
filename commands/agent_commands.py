"""
Agent 命令处理器
集成 zhiwei_agent 轻量级框架
"""
import sys
from pathlib import Path

# 添加 zhiwei_agent 路径
sys.path.insert(0, str(Path.home() / "zhiwei_agent"))

try:
    from zhiwei_agent.executor import AgentExecutor
    from zhiwei_agent.schemas import TaskLayer
    _agent_executor = AgentExecutor()
    _agent_available = True
except ImportError:
    _agent_available = False
    print("⚠️ zhiwei_agent 未安装，Agent 功能不可用")


def handle_agent_commands(text_lower, text_stripped, user_id, message_id, ctx):
    """
    处理 Agent 命令

    支持的命令:
    - /agent <消息> - 显式使用 Agent 处理
    - 自动路由所有 Layer 2 任务
    - 普通对话也通过 Agent 框架处理 ⭐ v55.0 改进
    """

    # 检查是否可用
    if not _agent_available:
        if text_lower.startswith("/agent "):
            ctx.reply_message(message_id, "⚠️ Agent 功能暂不可用")
            return True
        return False

    # 1. 显式 /agent 命令
    if text_lower.startswith("/agent "):
        query = text_stripped[7:].strip()
        return _execute_agent(query, user_id, message_id, ctx)

    # 2. 获取意图
    intent = _agent_executor.get_intent(text_stripped)

    # 3. Layer 2 任务：执行工作流
    if intent.layer == TaskLayer.LAYER_2 and intent.workflow:
        return _execute_agent(text_stripped, user_id, message_id, ctx)

    # 4. Layer 3 任务：转发给 OpenClaw
    if intent.layer == TaskLayer.LAYER_3 and intent.agent:
        ctx.reply_message(message_id, f"🔄 正在转发给 {intent.agent}...")
        if hasattr(ctx, 'call_openclaw_agent') and ctx.call_openclaw_agent:
            ctx.call_openclaw_agent(intent.agent, text_stripped, user_id)
        else:
            ctx.reply_message(message_id, "⚠️ OpenClaw 连接不可用")
        return True

    # 5. ⭐ v55.0 改进：普通对话也通过 Agent 处理
    # 返回 False 让后续 chat_handler 处理（保持兼容）
    return False


def _execute_agent(query, user_id, message_id, ctx):
    """执行 Agent 并返回结果"""

    ctx.reply_message(message_id, "🤔 思考中...")

    try:
        result = _agent_executor.execute(query)

        if result.success:
            # 格式化响应
            response = result.message
            if result.data and result.data.get("workflow"):
                workflow = result.data.get("workflow")
                steps = result.data.get("steps", [])
                duration = result.data.get("duration_ms", 0)
                response += f"\n\n_工作流: {workflow} | 步骤: {' → '.join(steps)} | 耗时: {duration}ms_"

            ctx.reply_message(message_id, response)
        else:
            ctx.reply_message(message_id, f"❌ 处理失败: {result.message}")

    except Exception as e:
        ctx.reply_message(message_id, f"❌ Agent 执行异常: {str(e)}")

    return True