#!/usr/bin/env python3
"""
命令处理上下文
封装所有外部依赖，解决全局变量耦合问题

使用方式:
    context = CommandContext(
        reply_message=...,
        reply_card=...,
        ...
    )
    handler = CommandHandler(context)
    handler.handle_text_async(text, user_id, message_id)
"""

from dataclasses import dataclass, field
from typing import Callable, Dict, Any, Optional
from collections import defaultdict


@dataclass
class CommandContext:
    """命令处理上下文 - 封装所有外部依赖"""

    # ========== 消息回复 ==========
    reply_message: Callable[[str, str], None] = None
    reply_card: Callable[[str, str, str], None] = None

    # ========== Agent 调用 ==========
    call_openclaw_agent: Callable[[str, str, str], str] = None
    get_chat_handler: Callable[[], Any] = None

    # ========== 知识库 ==========
    query_knowledge_base: Callable[[str], str] = None

    # ========== 记忆管理 ==========
    get_memory: Callable[[str], Any] = None
    add_to_history: Callable[[str, str, str], None] = None
    get_history: Callable[[str], str] = None

    # ========== URL 处理 ==========
    is_article_url: Callable[[str], bool] = None
    is_video_url: Callable[[str], bool] = None
    summarize_url: Callable[[str], str] = None
    handle_video_async: Callable[[str, str, str], None] = None
    extract_video_url: Callable[[str], str] = None
    extract_article_url: Callable[[str], str] = None

    # ========== 任务日志 ==========
    TaskLogger: Any = None

    # ========== 用户状态 ==========
    save_active_user: Callable[[str], None] = None
    load_active_user: Callable[[], str] = None

    # ========== 协作链 ==========
    detect_chain_intent: Callable[[str], Optional[str]] = None
    execute_chain: Callable[[str, str, str], str] = None

    # ========== 配置 ==========
    MAX_HISTORY: int = 20
    RATE_LIMIT_SECONDS: int = 2

    # ========== 运行时状态（可变） ==========
    chat_history: Dict[str, Any] = field(default_factory=dict)
    pending_voice: Dict[str, Any] = field(default_factory=dict)
    pending_image: Dict[str, Any] = field(default_factory=dict)
    pending_review: Dict[str, int] = field(default_factory=dict)
    pending_video_confirm: Dict[str, Any] = field(default_factory=dict)
    user_last_request: Dict[str, float] = field(default_factory=lambda: defaultdict(float))
    memory_cache: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> list[str]:
        """验证必需的依赖是否已设置"""
        required = [
            'reply_message',
            'get_memory',
            'add_to_history',
            'get_history',
        ]
        missing = []
        for attr in required:
            if getattr(self, attr) is None:
                missing.append(attr)
        return missing

    @property
    def has_chat_handler(self) -> bool:
        """检查是否有 chat_handler"""
        return self.get_chat_handler is not None

    @property
    def has_openclaw(self) -> bool:
        """检查是否有 OpenClaw Agent"""
        return self.call_openclaw_agent is not None

    def call_agent(self, message: str, session_id: str, agent: str = "main") -> str:
        """统一的 Agent 调用接口"""
        if self.get_chat_handler:
            handler = self.get_chat_handler()
            return handler.handle_sync(message, session_id, role=agent)
        elif self.call_openclaw_agent:
            return self.call_openclaw_agent(message, session_id, agent=agent)
        else:
            raise RuntimeError("No agent backend available")


# 全局上下文实例（向后兼容）
_global_context: Optional[CommandContext] = None


def get_context() -> CommandContext:
    """获取全局上下文"""
    global _global_context
    if _global_context is None:
        _global_context = CommandContext()
    return _global_context


def set_context(context: CommandContext):
    """设置全局上下文"""
    global _global_context
    _global_context = context


def init_context_from_globals(
    reply_message=None,
    reply_card=None,
    call_openclaw_agent=None,
    query_knowledge_base=None,
    get_memory=None,
    add_to_history=None,
    get_history=None,
    is_article_url=None,
    is_video_url=None,
    summarize_url=None,
    handle_video_async=None,
    extract_video_url=None,
    extract_article_url=None,
    TaskLogger=None,
    save_active_user=None,
    load_active_user=None,
    chat_history=None,
    pending_voice=None,
    pending_image=None,
    pending_review=None,
    MAX_HISTORY=20,
    RATE_LIMIT_SECONDS=2,
    user_last_request=None,
    memory_cache=None,
    get_chat_handler=None,
    pending_video_confirm=None,
    detect_chain_intent=None,
    execute_chain=None,
):
    """从全局变量初始化上下文（向后兼容 init_command_handler）"""
    context = CommandContext(
        reply_message=reply_message,
        reply_card=reply_card,
        call_openclaw_agent=call_openclaw_agent,
        get_chat_handler=get_chat_handler,
        query_knowledge_base=query_knowledge_base,
        get_memory=get_memory,
        add_to_history=add_to_history,
        get_history=get_history,
        is_article_url=is_article_url,
        is_video_url=is_video_url,
        summarize_url=summarize_url,
        handle_video_async=handle_video_async,
        extract_video_url=extract_video_url,
        extract_article_url=extract_article_url,
        TaskLogger=TaskLogger,
        save_active_user=save_active_user,
        load_active_user=load_active_user,
        detect_chain_intent=detect_chain_intent,
        execute_chain=execute_chain,
        MAX_HISTORY=MAX_HISTORY,
        RATE_LIMIT_SECONDS=RATE_LIMIT_SECONDS,
        chat_history=chat_history or {},
        pending_voice=pending_voice or {},
        pending_image=pending_image or {},
        pending_review=pending_review or {},
        pending_video_confirm=pending_video_confirm or {},
        user_last_request=user_last_request or defaultdict(float),
        memory_cache=memory_cache or {},
    )
    set_context(context)
    return context