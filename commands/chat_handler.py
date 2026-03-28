import json
import logging
import time
from zhiwei_common.llm import llm_client

# ⭐ v55.0: 移除老版意图识别器，统一使用 Agent 框架
# from core.intent_recognizer import IntentRecognizer

logger = logging.getLogger(__name__)

class ChatHandler:
    def __init__(self, reply_message, reply_card, get_memory, add_to_history):
        self.reply_message = reply_message
        self.reply_card = reply_card
        self.get_memory = get_memory
        self.add_to_history = add_to_history
        # ⭐ v55.0: 移除老版意图识别器
        # self.recognizer = IntentRecognizer()

    def handle_chat_message(self, text_stripped, user_id, message_id, session_id):
        """处理常规对话与意图推荐"""
        
        # 记录历史
        self.add_to_history(user_id, "user", text_stripped)
        
        # 获取记忆与上下文
        memory = self.get_memory(user_id)
        context_prompt = memory.build_context_prompt()
        
        # 如果问题中包含明显查资料意图，自动补充 RAG 结果
        rag_context = ""
        rag_triggers = ["查一下", "搜一下", "知识库", "库里", "文档", "书中", "书里"]
        if any(keyword in text_stripped for keyword in rag_triggers):
             # 此处需要调用核心 RAG
             from rag_bridge import get_context
             rag_result = get_context(text_stripped)
             if rag_result:
                 rag_context = f"\n\n【参考资料】\n{rag_result}\n"

        # 构造增强消息
        enriched_message = ""
        if context_prompt: enriched_message += f"{context_prompt}\n\n"
        if rag_context: enriched_message += f"{rag_context}\n\n"
        enriched_message += f"---\n当前问题: {text_stripped}"
        if rag_context: enriched_message += "\n(请结合参考资料回答)"

        # 调用 LLM
        response = llm_client.call_with_session("chat", enriched_message, session_id)

        # ⭐ v55.0: 移除老版意图识别器，研究意图由 Agent 框架处理
        # 普通对话不再触发研究卡片，用户可使用显式指令

        # 更新记忆并返回
        memory.add_turn(text_stripped, response)
        self.add_to_history(user_id, "bot", response)
        self.reply_message(message_id, response)
        return True
