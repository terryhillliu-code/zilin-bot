"""
普通对话处理模块
替代 call_openclaw_agent()，直连百炼 API

特性：
- 异步非阻塞，不阻塞 WebSocket 线程
- Prompt 资产化（存放在 prompts/ 目录）
- 记忆管理（通过 memory_manager）
- RAG 增强（通过 zhiwei-rag，可选降级）
- 问题分解（复杂问题自动拆分）⭐ v46.0 新增
- 意图识别（NLU 驱动研究触发）⭐ v47.0 新增

使用：
    from chat_handler import chat_handler

    # 异步调用
    response = await chat_handler.handle("你好", "session-123")

    # 同步包装（兼容旧代码）
    response = chat_handler.handle_sync("你好", "session-123")
"""
import os
import sys
import asyncio
import logging
from pathlib import Path
from typing import Optional

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class ChatHandler:
    """
    普通对话处理器

    核心方法：
    - handle(message, session_id) -> str  # 异步
    - handle_sync(message, session_id) -> str  # 同步包装

    v46.0 新增：
    - 问题分解能力（复杂问题自动拆分为子问题）

    v47.0 新增：
    - 意图识别（NLU 驱动研究触发）
    """

    def __init__(
        self,
        prompts_dir: Optional[str] = None,
        enable_rag: bool = True,
        enable_memory: bool = True,
        enable_decompose: bool = True,  # ⭐ v46.0 新增
        enable_intent: bool = True  # ⭐ v47.0 新增
    ):
        self.prompts_dir = Path(prompts_dir or Path(__file__).parent / "prompts")
        self.enable_rag = enable_rag
        self.enable_memory = enable_memory
        self.enable_decompose = enable_decompose
        self.enable_intent = enable_intent

        # 导入 LLM 客户端
        try:
            from llm_client import llm_client
            self.llm = llm_client
            logger.info("✅ LLM 客户端加载成功")
        except ImportError as e:
            logger.error(f"❌ LLM 客户端导入失败: {e}")
            self.llm = None

        # ⭐ v47.0 意图识别器
        self._intent_recognizer = None
        if self.enable_intent:
            try:
                from core.intent_recognizer import IntentRecognizer
                self._intent_recognizer = IntentRecognizer()
                logger.info("✅ 意图识别器加载成功")
            except ImportError as e:
                logger.warning(f"⚠️ 意图识别器不可用: {e}")
                self.enable_intent = False

        # 导入记忆管理器
        self._memory_manager_class = None
        if self.enable_memory:
            try:
                from memory_manager import MemoryManager
                self._memory_manager_class = MemoryManager
                logger.info("✅ 记忆管理器加载成功")
            except ImportError:
                logger.warning("⚠️ 记忆管理器不可用")
                self.enable_memory = False

        # 记忆管理器缓存
        self._memory_managers = {}

        # RAG 桥接
        self._rag_available = False
        if self.enable_rag:
            try:
                from api import retrieve
                self._rag_retrieve = retrieve
                self._rag_available = True
                logger.info("✅ RAG 桥接加载成功")
            except ImportError:
                logger.warning("⚠️ RAG 桥接不可用")
                self.enable_rag = False

        # 问题分解器 ⭐ v46.0 新增
        self._decomposer = None
        if self.enable_decompose and self.llm:
            try:
                from core.question_decomposer import QuestionDecomposer
                self._decomposer = QuestionDecomposer(self.llm)
                logger.info("✅ 问题分解器加载成功")
            except ImportError:
                logger.warning("⚠️ 问题分解器不可用")
                self.enable_decompose = False

        # 加载 system prompt
        self._system_prompt_cache = {}

    def _load_prompt(self, name: str) -> str:
        """加载 prompt 文件"""
        if name in self._system_prompt_cache:
            return self._system_prompt_cache[name]

        prompt_path = self.prompts_dir / f"{name}.md"
        if prompt_path.exists():
            with open(prompt_path, "r", encoding="utf-8") as f:
                content = f.read()
            self._system_prompt_cache[name] = content
            return content

        logger.warning(f"⚠️ Prompt 文件不存在: {prompt_path}")
        return ""

    def _get_memory_manager(self, session_id: str):
        """获取或创建记忆管理器"""
        if not self.enable_memory or not self._memory_manager_class:
            return None

        if session_id not in self._memory_managers:
            self._memory_managers[session_id] = self._memory_manager_class(
                user_id=session_id,
                max_working_rounds=6
            )

        return self._memory_managers[session_id]

    def _retrieve_context(self, query: str, top_k: int = 5) -> str:
        """
        RAG 检索上下文 (v47.8: 增强来源标注)

        Returns:
            检索结果文本，失败返回空字符串
        """
        if not self.enable_rag or not self._rag_available:
            return ""

        try:
            results = self._rag_retrieve(query, top_k=top_k)
            if not results:
                return ""

            context_parts = []
            for r in results[:3]:
                source = r.get('source', '')
                text = r.get('raw_text', r.get('text', ''))[:300]
                metadata = r.get('metadata', {})

                # v47.8: 构建来源标注
                citation = self._build_citation(source, metadata)
                context_parts.append(f"{citation}\n{text}")

            return "\n\n".join(context_parts)
        except Exception as e:
            logger.warning(f"⚠️ RAG 检索失败: {e}")
            return ""

    def _build_citation(self, source: str, metadata: dict) -> str:
        """
        构建来源标注 (v47.8)

        Args:
            source: 来源文件名
            metadata: 元数据（含 page, chunk_type, h1 等）

        Returns:
            格式化的引用标注，如 【PAPER_xxx.pdf, p.3】
        """
        # 提取关键信息
        page = metadata.get('page', 0)
        chunk_type = metadata.get('chunk_type', 'text')
        h1 = metadata.get('h1', '')

        # 简化 source 显示（去掉路径前缀）
        if '/' in source:
            source = source.split('/')[-1]

        # 构建标注
        parts = [source]

        # 添加页码（仅 PDF 类型）
        if page and page > 0:
            parts.append(f"p.{page}")

        # 添加章节信息（如果有）
        if h1 and len(h1) < 30:
            parts.append(f"「{h1}」")

        # 添加内容类型标记（非普通文本）
        if chunk_type and chunk_type != 'text':
            type_map = {
                'figure': '图',
                'table': '表',
                'code': '代码',
            }
            if chunk_type in type_map:
                parts.append(f"[{type_map[chunk_type]}]")

        return f"【{' '.join(parts)}】"

    def _build_message(
        self,
        user_message: str,
        context: str = "",
        memory_context: str = ""
    ) -> str:
        """构建完整消息"""

        parts = []

        # 记忆上下文
        if memory_context:
            parts.append(f"[对话历史]\n{memory_context}\n")

        # RAG 上下文
        if context:
            parts.append(f"[参考资料]\n{context}\n")

        # 用户消息
        parts.append(f"[用户消息]\n{user_message}")

        return "\n".join(parts)

    async def handle(
        self,
        message: str,
        session_id: str,
        role: str = "main"
    ) -> str:
        """
        处理普通对话（异步）

        Args:
            message: 用户消息
            session_id: 会话 ID
            role: 角色（main/researcher/operator）

        Returns:
            助手回复
        """
        if not self.llm:
            return "❌ 系统暂时不可用，请稍后重试"

        try:
            # ⭐ v47.0 修复：意图识别只对原始用户输入进行，而非 enriched_message
            # 从 enriched_message 中提取"当前问题"部分
            original_message = message
            if "---\n当前问题: " in message:
                # 提取当前问题部分
                original_message = message.split("---\n当前问题: ")[-1].split("\n")[0]

            # ⭐ v47.0 新增：意图识别
            if self.enable_intent and self._intent_recognizer:
                intent_result = self._intent_recognizer.recognize(original_message)
                if intent_result.is_research_intent():
                    logger.info(f"[ChatHandler] 检测到研究意图: {intent_result.entities.get('topic')}")
                    return await self._handle_research_intent(intent_result, session_id)

            # ⭐ v46.0 新增：问题分解
            if self.enable_decompose and self._decomposer:
                if self._decomposer.should_decompose(message):
                    return await self._handle_complex_question(message, session_id, role)

            # 直接回答（简单问题）
            return await self._answer_single(message, session_id, role)

        except Exception as e:
            logger.error(f"❌ 对话处理异常: {e}")
            return f"❌ 处理出错: {str(e)}"

    async def _handle_research_intent(
        self,
        intent_result,
        session_id: str
    ) -> str:
        """
        处理研究意图 (v47.0 新增)

        当检测到用户想要进行研究时，触发研究执行器。

        Args:
            intent_result: 意图识别结果
            session_id: 会话 ID

        Returns:
            研究确认消息或研究结果
        """
        entities = intent_result.entities
        topic = entities.get("topic", "")

        if not topic:
            return "请告诉我您想研究什么主题？"

        # 返回特殊标记，让 ws_client 或 command_handler 处理
        # 格式：[INTENT:RESEARCH]topic|include_videos|source
        include_videos = entities.get("include_videos")
        source = entities.get("source")

        params = [topic]
        if include_videos is not None:
            params.append(f"videos={'true' if include_videos else 'false'}")
        if source:
            params.append(f"source={source}")

        return f"[INTENT:RESEARCH]|{'|'.join(params)}"

    async def _answer_single(
        self,
        message: str,
        session_id: str,
        role: str = "main"
    ) -> str:
        """
        回答单个问题（内部方法）

        用于处理简单问题，或分解后的子问题。
        """
        try:
            # 1. 加载 system prompt (根据角色加载)
            prompt_name = f"{role}_agent" if role != "main" else "main_agent"
            system_prompt = self._load_prompt(prompt_name)
            if not system_prompt and role != "main":
                logger.warning(f"⚠️ 角色 {role} 的 Prompt 不存在，退化回 main_agent")
                system_prompt = self._load_prompt("main_agent")

            # 2. RAG 检索增强
            context = ""
            if self.enable_rag:
                context = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self._retrieve_context,
                    message
                )

            # 3. 获取对话记忆
            memory_context = ""
            if self.enable_memory:
                mm = self._get_memory_manager(session_id)
                if mm:
                    memory_context = mm.build_context_prompt()

            # 4. 构建完整消息
            full_message = self._build_message(message, context, memory_context)

            # 5. 调用 LLM（异步执行）
            success, response = await asyncio.get_event_loop().run_in_executor(
                None,
                self.llm.call,
                role,
                full_message,
                system_prompt
            )

            if not success:
                logger.error(f"LLM 调用失败: {response}")
                return "❌ 我暂时无法回答，请稍后重试"

            # 6. 保存记忆
            if self.enable_memory:
                mm = self._get_memory_manager(session_id)
                if mm:
                    mm.add_turn(message, response)

            return response

        except Exception as e:
            logger.error(f"❌ 单问题处理异常: {e}")
            return f"❌ 处理出错: {str(e)}"

    async def _handle_complex_question(
        self,
        message: str,
        session_id: str,
        role: str = "main"
    ) -> str:
        """
        处理复杂问题（分解模式）

        工作流程：
        1. 分解问题为子问题
        2. 逐一回答子问题
        3. 综合答案生成最终回复
        """
        logger.info(f"[ChatHandler] 进入问题分解模式: {message[:50]}...")

        # 1. 分解问题
        sub_questions = await asyncio.get_event_loop().run_in_executor(
            None,
            self._decomposer.decompose,
            message
        )

        if len(sub_questions) == 1:
            # 分解失败，退化为直接回答
            return await self._answer_single(message, session_id, role)

        # 2. 逐一回答子问题
        sub_answers = []
        for i, sq in enumerate(sub_questions):
            logger.info(f"[ChatHandler] 回答子问题 {i+1}/{len(sub_questions)}: {sq[:30]}...")
            answer = await self._answer_single(sq, session_id, role)
            sub_answers.append(answer)

        # 3. 综合答案
        logger.info("[ChatHandler] 综合各子问题答案...")
        final_response = await asyncio.get_event_loop().run_in_executor(
            None,
            self._decomposer.synthesize,
            message,
            sub_answers
        )

        # 4. 保存记忆（保存完整对话）
        if self.enable_memory:
            mm = self._get_memory_manager(session_id)
            if mm:
                mm.add_turn(message, final_response)

        return final_response

    def handle_sync(
        self,
        message: str,
        session_id: str,
        role: str = "main"
    ) -> str:
        """
        同步包装（兼容旧代码）

        用于在同步上下文中调用异步方法
        """
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        return loop.run_until_complete(
            self.handle(message, session_id, role)
        )

    def reset_memory(self, session_id: str):
        """重置会话记忆"""
        if self.enable_memory and session_id in self._memory_managers:
            self._memory_managers[session_id].reset()
            del self._memory_managers[session_id]


# 全局单例
chat_handler = ChatHandler()


# 便捷函数
async def handle_chat(message: str, session_id: str, role: str = "main") -> str:
    """便捷异步函数"""
    return await chat_handler.handle(message, session_id, role)


def handle_chat_sync(message: str, session_id: str, role: str = "main") -> str:
    """便捷同步函数"""
    return chat_handler.handle_sync(message, session_id, role)


# 测试
if __name__ == "__main__":
    print("=== 测试 ChatHandler ===")

    # 同步测试
    result = handle_chat_sync("你好，请自我介绍", "test-session-001")
    print(f"回复: {result[:200]}..." if len(result) > 200 else f"回复: {result}")