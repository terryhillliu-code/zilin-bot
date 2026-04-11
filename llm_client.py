"""兼容层：重定向到 zhiwei_common.llm"""
from zhiwei_common.llm import LLMClient, LLMConfig, call_llm, call_llm_with_session

# 创建全局实例（兼容旧代码）
llm_client = LLMClient()

__all__ = ['LLMClient', 'LLMConfig', 'call_llm', 'call_llm_with_session', 'llm_client']
