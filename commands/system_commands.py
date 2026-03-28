def handle_system_commands(text_lower, text_stripped, user_id, message_id, ctx):
    """处理 /help 等系统命令"""

    if text_lower == "/status":
        ctx.reply_message(message_id,
            "📌 系统状态已移交「探微」机器人\n\n"
            "请 @探微 或私聊探微发送：\n"
            "`状态` - 查看系统状态\n"
            "`/任务` - 查看任务队列\n\n"
            "💡 知微专注实时交互：对话、视频、图片、知识库"
        )
        return True

    if text_lower == "/help" or text_lower == "/帮助" or text_lower == "帮助":
        help_text = (
            "🤖 知微 - 个人 AI 助手\n\n"
            "💬 对话交流\n"
            "  直接发文字即可对话\n\n"
            "🎬 媒体处理\n"
            "  发送视频链接 → 自动生成知识笔记\n"
            "  发送图片 → 图片分析与问答\n"
            "  发送语音 → 转文字并提取任务\n\n"
            "🔍 知识库\n"
            "  /ask <问题> - 检索本地知识库\n\n"
            "🛠️ 开发工作流\n"
            "  /dev <需求> - 触发自主开发\n"
            "  /accept <id> - 审批通过\n"
            "  /reject <id> - 审批拒绝\n\n"
            "───────────────\n"
            "📌 信息与任务 → 请找「探微」\n"
            "  /research, /notebooklm, /status, 新闻, 早报"
        )
        ctx.reply_message(message_id, help_text)
        return True

    return False
