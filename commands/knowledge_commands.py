from zhiwei_common.llm import llm_client
import time
from pathlib import Path

def handle_knowledge_commands(text_lower, text_stripped, user_id, message_id, ctx):
    """处理 /ask, /klib, /收录, /insight 等知识库命令"""

    # /insight 灵感捕获 → KarpathyVault
    if text_lower.startswith("/insight ") or text_lower.startswith("/闪念 "):
        content = text_stripped.split(" ", 1)[1] if " " in text_stripped else ""
        if not content:
            ctx.reply_message(message_id, "❌ 请提供灵感内容\n\n用法: /insight 这篇论文的互连拓扑刚好可以解决...")
            return True

        # 写入 KarpathyVault/_raw/
        vault_path = Path.home() / "KarpathyVault" / "_raw"
        vault_path.mkdir(parents=True, exist_ok=True)

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        filename = f"raw_insight_{timestamp}.md"
        filepath = vault_path / filename

        file_content = f"""# 灵感捕获

> 时间: {time.strftime("%Y-%m-%d %H:%M:%S")}
> 来源: 飞书 /insight
> 用户: {user_id}

{content}
"""

        try:
            filepath.write_text(file_content, encoding="utf-8")
            ctx.reply_message(message_id, f"✅ 灵感已捕获\n\n📄 `{filename}`\n等待 Ingest 处理")
            return True
        except Exception as e:
            ctx.reply_message(message_id, f"❌ 写入失败: {e}")
            return True

    # /ask 知识库检索
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
