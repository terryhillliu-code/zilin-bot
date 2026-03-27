def handle_system_commands(text_lower, text_stripped, user_id, message_id, ctx):
    """处理 /status, /health, /help 等系统命令"""
    
    if text_lower == "/status":
        from core.health_check import get_system_health_dict, format_health_status
        status_dict = get_system_health_dict()
        ctx.reply_message(message_id, format_health_status(status_dict))
        return True
        
    if text_lower == "/help" or text_lower == "/帮助" or text_lower == "帮助":
        help_text = (
            "🤖 知微系统指令帮助\n\n"
            "🔍 研究与分析:\n"
            "  /research <内容>      深度研究并产出周报格式研报\n"
            "  /notebooklm <内容>    生成深度研究笔记\n"
            "  /ask <问题>           查询本地知识库\n\n"
            "🛠️ 开发与运维:\n"
            "  /dev <内容>           触发自主开发工作流\n"
            "  /accept <id>          审批并通过开发修改\n"
            "  /status               检查系统健康状态\n\n"
            "📋 任务管理:\n"
            "  /任务                 查看待处理任务\n\n"
            "💡 直接对话知微，可处理视频链接、图片或常规对话。"
        )
        ctx.reply_message(message_id, help_text)
        return True

    return False
