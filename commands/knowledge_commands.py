from zhiwei_common.llm import llm_client
import time
from pathlib import Path

# ============ 共享函数（NL 路由器与斜杠命令共用，2026-07-26 P1） ============


def do_capture(content: str, user_id: str, source: str = "飞书 /insight") -> tuple:
    """把灵感/知识片段写入 KarpathyVault/_raw/

    Returns: (success, filepath_or_error, filename)
    """
    vault_path = Path.home() / "KarpathyVault" / "_raw"
    vault_path.mkdir(parents=True, exist_ok=True)

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    filename = f"raw_insight_{timestamp}.md"
    filepath = vault_path / filename

    file_content = f"""# 灵感捕获

> 时间: {time.strftime("%Y-%m-%d %H:%M:%S")}
> 来源: {source}
> 用户: {user_id[:8] if len(user_id) > 8 else user_id}

{content}
"""
    try:
        filepath.write_text(file_content, encoding="utf-8")
        return True, str(filepath), filename
    except Exception as e:
        return False, str(e), filename


def do_knowledge_query(query: str, user_id: str, message_id: str, ctx, deep: bool = False) -> bool:
    """知识库问答：rag_client HTTP 检索 + LLM 回答。deep=True 用扩展检索（研究确认路径）"""
    ctx.reply_message(message_id, "🔍 正在检索知识库...")

    from core.rag_client import get_rag_client
    context = get_rag_client().get_context(query, top_k=10 if deep else 5)

    if not context:
        ctx.reply_message(message_id, "💡 知识库中未找到相关直接内容，知微将尝试根据通用知识回答。")
        prompt = query
    else:
        prefix = "【参考资料（扩展检索）】" if deep else "【参考资料】"
        extra = "\n\n要求：综合上述材料给出有组织的要点，注明来源文档，并指出信息缺口。" if deep else ""
        prompt = f"{prefix}\n{context}\n\n问题：{query}{extra}"

    response = llm_client.call_with_session("chat", prompt, f"ask-{user_id}")
    ctx.reply_message(message_id, response)
    return True


# ============ 斜杠命令（行为不变，委托共享函数） ============


def handle_knowledge_commands(text_lower, text_stripped, user_id, message_id, ctx):
    """处理 /ask, /klib, /收录, /insight 等知识库命令"""

    # /insight 灵感捕获 → KarpathyVault
    if text_lower.startswith("/insight ") or text_lower.startswith("/闪念 "):
        content = text_stripped.split(" ", 1)[1] if " " in text_stripped else ""
        if not content:
            ctx.reply_message(message_id, "❌ 请提供灵感内容\n\n用法: /insight 这篇论文的互连拓扑刚好可以解决...")
            return True

        ok, info, filename = do_capture(content, user_id)
        if ok:
            ctx.reply_message(message_id, f"✅ 灵感已捕获\n\n📄 `{filename}`\n等待 Ingest 处理")
        else:
            ctx.reply_message(message_id, f"❌ 写入失败: {info}")
        return True

    # /ask 知识库检索
    if text_lower.startswith("/ask ") or text_lower.startswith("查一下 "):
        query = text_stripped.split(" ", 1)[1] if " " in text_stripped else ""
        if not query:
            ctx.reply_message(message_id, "❌ 请提供查询内容")
            return True
        do_knowledge_query(query, user_id, message_id, ctx)
        return True

    return False
