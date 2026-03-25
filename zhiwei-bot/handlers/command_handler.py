import logging

logger = logging.getLogger(__name__)

def handle_command(text, context):
    """
    智能研究员「知微」的命令处理器
    """
    if text.startswith('/research'):
        query = text.replace('/research', '').strip()
        if not query:
            return "请输入研究主题，例如：/research 具身智能"
        logger.info(f"🚀 启动专题研究: {query}")
        return f"🔍 **已启动深度调研：{query}**\n\n知微正在为您检索 ArXiv 论文、本地 Obsidian 笔记并将成果推送至您的研究频道。此过程通常需要 1-2 分钟。"
    
    if text == '/help':
        return (
            "👋 您好！我是您的智能研究助理「知微」。\n\n"
            "我可以为您提供以下帮助：\n"
            "1. **学术对话**：直接跟我聊天，探讨任何技术或研究问题。\n"
            "2. **知识检索**：在对话中包含“笔记”、“查下”等词汇，我将检索您的本地库。\n"
            "3. **专题研究**：发送 `/research [主题]` 启动自动化研报生成。\n\n"
            "💡 如需查看系统状态，请咨询我的同事「探微」机器人。"
        )
    
    return None
