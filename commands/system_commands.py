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
        # ⭐ S3 2026-07-31 飞书入口收敛：
        # 依据——message_log 全量 24 条入站消息中，贴链接占 88%，斜杠命令 0%。
        # 旧文案还在推 /dev（dev worker 已 7/25 退役）与"请找探微"（feishu-gateway
        # 已 7/25 归档），照着做会调用不存在的能力。现改为只讲真实可用的三件事：
        # 贴链接 / 直接说话 / 主动推送；其余能力引向 Web 控制台。
        # 斜杠命令保留作兜底入口（删除要改路由链、收益为零），但不再宣传。
        help_text = (
            "🤖 知微\n\n"
            "直接把东西扔给我就行，不用记命令。\n\n"
            "🔗 发链接（最常用）\n"
            "  视频 / 文章链接 → 自动生成知识笔记，完成后回推要点摘要\n"
            "  链接 + 「视觉分析」→ 额外抽帧提取图表重蒸\n\n"
            "💬 直接说话\n"
            "  “知识库里 HBM4 的笔记有哪些” → 检索本地知识库\n"
            "  “记一下：……” → 存入知识库\n"
            "  “帮我深入研究 CXL 内存池化” → 扩展检索 + 整理\n"
            "  “系统状态怎么样” → 返回健康摘要\n\n"
            "🖼️ 发图片 / 语音\n"
            "  图片 → 内容分析，之后可直接追问（同一张图多轮）\n"
            "  语音 → 转文字并提取待办\n\n"
            "📨 自动推送\n"
            "  早报 / 情报日报 / 周报 / 冷启动自检 无需请求\n\n"
            "───────────────\n"
            "🖥️ 完整功能在电脑控制台：\n"
            "  http://127.0.0.1:8898/console\n"
            "  总览 / 投喂 / 知识检索 / 论文 / 订阅 / 系统"
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
