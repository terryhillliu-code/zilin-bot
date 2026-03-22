"""全链路模拟测试：智能研究报告流"""
import os
import sys
import asyncio
from pathlib import Path

# 添加路径
sys.path.insert(0, str(Path(__file__).parent))

from command_handler import handle_text_async, init_command_handler
from command_context import get_context

def mock_reply_message(message_id, content):
    print(f"\n[MOCK REPLY MESSAGE] ID: {message_id}\n内容: {content}\n")

def mock_reply_card(message_id, title, content):
    print(f"\n[MOCK REPLY CARD] ID: {message_id}\n标题: {title}\n内容:\n{content}\n")

async def run_test():
    print("🚀 启动语义研究全链路测试...")
    
    # 1. 初始化 CommandContext (模拟 ws_client.py 的初始化过程)
    from task_logger import TaskLogger
    from intent_router import IntentRouter
    
    # 定义 Mock 的依赖函数
    def mock_call_agent(msg, sid, role="main"): 
        return "[ACTION: RESEARCH_REPORT][TOPIC: MoE] 好的，我这就为您准备素材。"
    
    class MockChatHandler:
        def handle_sync(self, msg, sid, role="main"):
            if "MoE" in msg or role == "researcher":
                return "[ACTION: RESEARCH_REPORT][TOPIC: MoE] 好的，我识别到您需要关于 MoE 的研究报告，正在为您打包素材。"
            return "我是知微，请问有什么可以帮您？"
    
    chat_handler_instance = MockChatHandler()
    
    def mock_query_rag(q): return "找到了一些 MoE 相关的论文。"
    def mock_get_mem(uid): 
        class MockMem:
            def build_context_prompt(self): return ""
            def add_turn(self, q, r): pass
            def save_persistent(self, k, v): pass
        return MockMem()
    def mock_no_op(*args, **kwargs): return False
    def mock_get_history(uid): return ""

    init_command_handler(
        global_reply_message=mock_reply_message,
        global_reply_card=mock_reply_card,
        global_call_openclaw_agent=mock_call_agent,
        global_query_knowledge_base=mock_query_rag,
        global_get_memory=mock_get_mem,
        global_add_to_history=mock_no_op,
        global_get_history=mock_get_history,
        global_is_article_url=mock_no_op,
        global_is_video_url=mock_no_op,
        global_summarize_url=mock_no_op,
        global_handle_video_async=mock_no_op,
        global_extract_video_url=mock_no_op,
        global_extract_article_url=mock_no_op,
        global_TaskLogger=TaskLogger,
        global_IntentRouter=IntentRouter,
        global_save_active_user=mock_no_op,
        global_load_active_user=lambda: "test_user",
        global_chat_history={},
        global_pending_voice={},
        global_pending_image={},
        global_pending_review={},
        global_MAX_HISTORY=20,
        global_RATE_LIMIT_SECONDS=0,
        global_user_last_request={},
        global_memory_cache={},
        global_get_chat_handler=lambda: chat_handler_instance
    )
    
    # 2. 模拟用户输入 (重构 v2.0 指令测试)
    test_user = "test_user_001"
    test_msg_id = "mock_msg_2.0"
    test_text = "/notebooklm Mixture of Depths --template=tech_comparison"
    
    print(f"用户输入: {test_text}")
    print("-" * 50)
    
    # 3. 触发处理 (异步)
    # 注意：handle_text_async 内部会启动线程，我们要等待它完成
    handle_text_async(test_text, test_user, test_msg_id)
    
    # 等待执行完成 (因为内部有线程执行导出和 LLM 回复)
    print("⏳ 等待 AI 思考与导出逻辑执行 (30s)...")
    await asyncio.sleep(30)
    print("\n✅ 测试结束")

if __name__ == "__main__":
    asyncio.run(run_test())
