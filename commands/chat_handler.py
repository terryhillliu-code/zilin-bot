import json
import logging
import time
import subprocess
import os

# ⭐ v55.7: 集成 Mem0 长期记忆系统
logger = logging.getLogger(__name__)

# OpenClaw 容器名称
OPENCLAW_CONTAINER = "clawdbot"

# Mem0 记忆系统（延迟导入）
_mem0_client = None

def _get_mem0():
    """获取 Mem0 客户端"""
    global _mem0_client
    if _mem0_client is None:
        try:
            from core.mem0_client import get_mem0, search_memory, build_memory_context
            _mem0_client = {
                "get": get_mem0,
                "search": search_memory,
                "build_context": build_memory_context
            }
        except ImportError as e:
            logger.warning(f"Mem0 导入失败: {e}")
    return _mem0_client


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
        """处理常规对话 - v55.7 集成 Mem0 长期记忆"""

        # 记录历史
        self.add_to_history(user_id, "user", text_stripped)

        # ⭐ v55.7: 构建 Mem0 长期记忆上下文
        mem0_context = ""
        mem0 = _get_mem0()
        if mem0:
            try:
                mem0_context = mem0["build_context"](user_id, text_stripped, limit=5)
                if mem0_context:
                    logger.info(f"[Mem0] 找到 {len(mem0_context.split(chr(10)))} 条相关记忆")
            except Exception as e:
                logger.warning(f"[Mem0] 搜索记忆失败: {e}")

        # 构建 RAG 上下文（保留知识库检索能力）
        rag_context = ""
        rag_triggers = ["查一下", "搜一下", "知识库", "库里", "文档", "书中", "书里"]
        if any(keyword in text_stripped for keyword in rag_triggers):
            try:
                from rag_bridge import get_context
                rag_result = get_context(text_stripped)
                if rag_result:
                    rag_context = f"\n\n【参考资料】\n{rag_result}\n(请结合参考资料回答)"
            except ImportError:
                pass

        # 构建增强消息
        enriched_parts = []
        if mem0_context:
            enriched_parts.append(mem0_context)
        if rag_context:
            enriched_parts.append(rag_context)
        enriched_parts.append(f"\n当前问题: {text_stripped}")

        enriched_message = "\n".join(enriched_parts)

        # ⭐ 优先使用 OpenClaw（有 session 记忆）
        success, response = _call_openclaw_agent(enriched_message, session_id, agent="main")

        if not success:
            # 降级：使用本地 LLM + memory_manager
            logger.warning(f"OpenClaw 调用失败: {response}，降级到本地 LLM")
            from zhiwei_common.llm import llm_client

            # 获取本地记忆上下文
            memory = self.get_memory(user_id)
            context_prompt = memory.build_context_prompt()
            full_message = f"{context_prompt}\n\n{enriched_message}" if context_prompt else enriched_message

            response = llm_client.call_with_session("chat", full_message, session_id)

            # 更新本地记忆
            memory.add_turn(text_stripped, response)

        # ⭐ v55.7: 保存对话到 Mem0 长期记忆
        if mem0:
            try:
                from core.mem0_client import add_conversation_memory
                add_conversation_memory(text_stripped, response, user_id)
                logger.info(f"[Mem0] 已保存对话记忆")
            except Exception as e:
                logger.warning(f"[Mem0] 保存记忆失败: {e}")

        # 记录历史并回复
        self.add_to_history(user_id, "bot", response)
        self.reply_message(message_id, response)
        return True
