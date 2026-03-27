from zhiwei_common.llm import llm_client

def handle_knowledge_commands(text_lower, text_stripped, user_id, message_id, ctx):
    """处理 /ask, /klib, /收录 等知识库命令"""
    
    if text_lower.startswith("/ask ") or text_lower.startswith("查一下 "):
        query = text_stripped.split(" ", 1)[1] if " " in text_stripped else ""
        if not query:
            ctx.reply_message(message_id, "❌ 请提供查询内容")
            return True
            
        ctx.reply_message(message_id, "🔍 正在检索知识库...")
        
        # 调用 RAG 检索
        from rag_bridge import get_context
        context = get_context(query)
        
        if not context:
            ctx.reply_message(message_id, "💡 知识库中未找到相关直接内容，知微将尝试根据通用知识回答。")
            prompt = query
        else:
            prompt = f"【参考资料】\n{context}\n\n问题：{query}"
            
        # 调用 LLM 回答
        response = llm_client.call_with_session("chat", prompt, f"ask-{user_id}")
        ctx.reply_message(message_id, response)
        return True

    return False
