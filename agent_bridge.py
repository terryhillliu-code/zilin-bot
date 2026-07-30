"""OpenClaw Agent 调用桥（2026-07-30 从 ws_client.py 拆分）

真正调用 OpenClaw 容器的 agent 命令；不可用时降级到 chat_handler。
"""

import json
import os
import subprocess

# V2-203: 导入 ChatHandler 替代 OpenClaw
from chat_handler import ChatHandler, chat_handler as _chat_handler_instance


def get_chat_handler():
    """获取 ChatHandler 实例"""
    return _chat_handler_instance


def call_openclaw_agent(message: str, session_id: str, agent: str = "main") -> str:
    """
    调用 OpenClaw Agent（真正的执行层）

    v55.6 修复架构断裂：
    - 真正调用 OpenClaw 容器的 agent 命令
    - 如果 OpenClaw 不可用，降级到 chat_handler
    """
    import logging
    logger = logging.getLogger(__name__)

    # kill-switch: OPENCLAW_ENABLED=0 时直接走 chat_handler
    if os.getenv("OPENCLAW_ENABLED", "1") != "1":
        logger.info("OpenClaw 已禁用(OPENCLAW_ENABLED=0),直接走 chat_handler")
        handler = get_chat_handler()
        return handler.handle_sync(message, session_id, role=agent)

    try:
        # 构建命令
        cmd = [
            "docker", "exec", "clawdbot",
            "openclaw", "agent",
            "--agent", agent,
            "--message", message,
            "--session-id", f"zhiwei-{session_id}",
            "--json"
        ]

        # 调用 OpenClaw
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120  # 2 分钟超时
        )

        if result.returncode != 0:
            logger.warning(f"OpenClaw 返回错误: {result.stderr[:200]}")
            # 降级到 chat_handler
            handler = get_chat_handler()
            return handler.handle_sync(message, session_id, role=agent)

        # 解析 JSON 结果
        try:
            data = json.loads(result.stdout)
            if data.get("status") == "ok" and data.get("result"):
                payloads = data["result"].get("payloads", [])
                if payloads and payloads[0].get("text"):
                    return payloads[0]["text"]
        except json.JSONDecodeError as e:
            logger.warning(f"OpenClaw JSON 解析失败: {e}")

        # 降级到 chat_handler
        handler = get_chat_handler()
        return handler.handle_sync(message, session_id, role=agent)

    except subprocess.TimeoutExpired:
        logger.warning("OpenClaw 调用超时，降级到 chat_handler")
        handler = get_chat_handler()
        return handler.handle_sync(message, session_id, role=agent)

    except FileNotFoundError:
        logger.warning("Docker 命令不可用，降级到 chat_handler")
        handler = get_chat_handler()
        return handler.handle_sync(message, session_id, role=agent)

    except Exception as e:
        logger.error(f"OpenClaw 调用异常: {e}")
        handler = get_chat_handler()
        return handler.handle_sync(message, session_id, role=agent)
