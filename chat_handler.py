"""
普通对话处理模块
替代 call_openclaw_agent()，直连百炼 API

特性：
- 异步非阻塞，不阻塞 WebSocket 线程
- Prompt 资产化（存放在 prompts/ 目录）
- 记忆管理（通过 memory_manager）
- RAG 增强（通过 zhiwei-rag，可选降级）

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
    """

    def __init__(
        self,
        prompts_dir: Optional[str] = None,
        enable_rag: bool = True,
        enable_memory: bool = True
    ):
        self.prompts_dir = Path(prompts_dir or Path(__file__).parent / "prompts")
        self.enable_rag = enable_rag
        self.enable_memory = enable_memory

        # 导入 LLM 客户端
        try:
            from llm_client import llm_client
            self.llm = llm_client
            logger.info("✅ LLM 客户端加载成功")
        except ImportError as e:
            logger.error(f"❌ LLM 客户端导入失败: {e}")
            self.llm = None

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
                sys.path.insert(0, str(Path.home() / "zhiwei-rag"))
                from api import retrieve
                self._rag_retrieve = retrieve
                self._rag_available = True
                logger.info("✅ RAG 桥接加载成功")
            except ImportError:
                logger.warning("⚠️ RAG 桥接不可用")
                self.enable_rag = False

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
        RAG 检索上下文

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
                context_parts.append(f"【{source}】\n{text}")

            return "\n\n".join(context_parts)
        except Exception as e:
            logger.warning(f"⚠️ RAG 检索失败: {e}")
            return ""

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
            # 1. 加载 system prompt
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
            logger.error(f"❌ 对话处理异常: {e}")
            return f"❌ 处理出错: {str(e)}"

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