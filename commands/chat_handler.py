import json
import logging
import time
import subprocess
import os

# ⭐ v55.6: 优先使用 OpenClaw 记忆系统，降级到本地 LLM
logger = logging.getLogger(__name__)

# OpenClaw 容器名称
OPENCLAW_CONTAINER = "clawdbot"

def _call_openclaw_agent(message: str, session_id: str, agent: str = "main", timeout: int = 120) -> tuple:
    """
    调用 OpenClaw Agent（内部函数）

    Returns:
        (success: bool, response: str)
    """
    try:
        cmd = [
            "docker", "exec", OPENCLAW_CONTAINER,
            "openclaw", "agent",
            "--agent", agent,
            "--message", message,
            "--session-id", f"zhiwei-{session_id}",
            "--json"
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout
        )

        if result.returncode != 0:
            return False, result.stderr[:200] if result.stderr else "OpenClaw 返回错误"

        data = json.loads(result.stdout)
        if data.get("status") == "ok" and data.get("result"):
            payloads = data["result"].get("payloads", [])
            if payloads and payloads[0].get("text"):
                return True, payloads[0]["text"]

        return False, "OpenClaw 响应格式错误"

    except subprocess.TimeoutExpired:
        return False, "OpenClaw 超时"
    except FileNotFoundError:
        return False, "Docker 不可用"
    except Exception as e:
        return False, str(e)


class ChatHandler:
    def __init__(self, reply_message, reply_card, get_memory, add_to_history):
        self.reply_message = reply_message
        self.reply_card = reply_card
        self.get_memory = get_memory
        self.add_to_history = add_to_history

    def handle_chat_message(self, text_stripped, user_id, message_id, session_id):
        """处理常规对话 - v55.6 优先使用 OpenClaw 记忆系统"""

        # 记录历史
        self.add_to_history(user_id, "user", text_stripped)

        # 构建增强消息（保留 RAG 能力）
        enriched_message = text_stripped
        rag_triggers = ["查一下", "搜一下", "知识库", "库里", "文档", "书中", "书里"]
        if any(keyword in text_stripped for keyword in rag_triggers):
            try:
                from rag_bridge import get_context
                rag_result = get_context(text_stripped)
                if rag_result:
                    enriched_message = f"{text_stripped}\n\n【参考资料】\n{rag_result}\n(请结合参考资料回答)"
            except ImportError:
                pass

        # ⭐ v55.6: 优先使用 OpenClaw（有完整记忆系统）
        success, response = _call_openclaw_agent(enriched_message, session_id, agent="main")

        if not success:
            # 降级：使用本地 LLM + memory_manager
            logger.warning(f"OpenClaw 调用失败: {response}，降级到本地 LLM")
            from zhiwei_common.llm import llm_client

            # 获取本地记忆上下文
            memory = self.get_memory(user_id)
            context_prompt = memory.build_context_prompt()
            full_message = f"{context_prompt}\n\n{text_stripped}" if context_prompt else text_stripped

            response = llm_client.call_with_session("chat", full_message, session_id)

            # 更新本地记忆
            memory.add_turn(text_stripped, response)

        # 记录历史并回复
        self.add_to_history(user_id, "bot", response)
        self.reply_message(message_id, response)
        return True
