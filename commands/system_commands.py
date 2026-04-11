def handle_system_commands(text_lower, text_stripped, user_id, message_id, ctx):
    """处理 /help 等系统命令"""

    if text_lower == "/zw-status" or text_lower == "/zw状态":
        _handle_zw_status(message_id, ctx)
        return True

    if text_lower.startswith("/zw-log "):
        task_id = text_stripped.split(maxsplit=1)[1] if len(text_stripped.split()) > 1 else None
        if task_id:
            _handle_zw_log(message_id, task_id, ctx)
        return True

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
            "📊 任务监控 (v58.1)\n"
            "  /zw-status - 查看任务队列状态\n"
            "  /zw-log <id> - 查看任务详情\n\n"
            "───────────────\n"
            "📌 信息与任务 → 请找「探微」\n"
            "  /research, /notebooklm, /status, 新闻, 早报"
        )
        ctx.reply_message(message_id, help_text)
        return True

    return False


def _handle_zw_status(message_id, ctx):
    """v58.1: 查询 zhiwei-dev 任务队列状态"""
    import sqlite3
    from pathlib import Path

    db_path = Path.home() / "zhiwei-dev" / "tasks.db"
    heartbeat_path = Path("/tmp/zhiwei-dev-worker.heartbeat")

    try:
        # 读取心跳
        worker_status = "未知"
        if heartbeat_path.exists():
            import json
            hb = json.loads(heartbeat_path.read_text())
            worker_status = f"🟢 运行中 (PID: {hb.get('pid', '?')}, 任务: {hb.get('active_tasks', [])})"

        # 查询任务状态
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        # 执行中
        executing = conn.execute(
            "SELECT id, progress FROM tasks WHERE status = 'in_progress' ORDER BY id DESC LIMIT 5"
        ).fetchall()

        # 排队中
        pending = conn.execute(
            "SELECT id, input FROM tasks WHERE status = 'pending' ORDER BY id LIMIT 5"
        ).fetchall()

        # 待确认
        awaiting = conn.execute(
            "SELECT id, input FROM tasks WHERE status = 'awaiting_review' ORDER BY id DESC LIMIT 5"
        ).fetchall()

        # 最近完成/失败
        recent = conn.execute(
            "SELECT id, status, input FROM tasks WHERE status IN ('done', 'failed') ORDER BY id DESC LIMIT 5"
        ).fetchall()

        conn.close()

        # 构建消息
        lines = [f"📊 **知微开发队列状态**\n"]
        lines.append(f"Worker: {worker_status}\n")

        if executing:
            lines.append("🔄 **执行中:**")
            for r in executing:
                lines.append(f"  #{r['id']}: {r['progress'][:30]}...")

        if pending:
            lines.append(f"\n⏳ **排队:** {len(pending)} 个任务")

        if awaiting:
            lines.append("\n📋 **待确认:**")
            for r in awaiting:
                lines.append(f"  #{r['id']}: {r['input'][:25]}...")

        if recent:
            lines.append("\n📜 **最近完成:**")
            for r in recent:
                emoji = "✅" if r['status'] == 'done' else "❌"
                lines.append(f"  {emoji} #{r['id']}: {r['input'][:20]}...")

        if not any([executing, pending, awaiting, recent]):
            lines.append("\n💤 队列为空")

        ctx.reply_message(message_id, "\n".join(lines))

    except Exception as e:
        ctx.reply_message(message_id, f"❌ 查询失败: {e}")


def _handle_zw_log(message_id, task_id_str, ctx):
    """v58.1: 查询任务详情"""
    from pathlib import Path

    try:
        task_id = int(task_id_str)
    except ValueError:
        ctx.reply_message(message_id, "❌ 任务 ID 格式错误，请输入数字")
        return

    artifacts_dir = Path.home() / "zhiwei-dev" / "artifacts" / str(task_id)

    if not artifacts_dir.exists():
        ctx.reply_message(message_id, f"❌ 任务 #{task_id} 不存在")
        return

    # 读取诊断报告
    diag_file = artifacts_dir / "diagnosis.md"
    if diag_file.exists():
        content = diag_file.read_text()[:1500]
        ctx.reply_message(message_id, f"📋 **任务 #{task_id} 诊断报告**\n\n{content}")
        return

    # 读取运行日志
    run_log = artifacts_dir / "run.log"
    if run_log.exists():
        lines = run_log.read_text().split("\n")
        last_30 = "\n".join(lines[-30:])
        ctx.reply_message(message_id, f"📋 **任务 #{task_id} 日志**\n\n```\n{last_30}\n```")
        return

    ctx.reply_message(message_id, f"⚠️ 任务 #{task_id} 无日志文件")
