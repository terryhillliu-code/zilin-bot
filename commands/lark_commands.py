"""
飞书操作命令处理器 ⭐ v57.0

提供 /lark 命令显式调用飞书功能
"""
import sys
from pathlib import Path

# 添加 zhiwei_agent 路径
sys.path.insert(0, str(Path.home() / "zhiwei_agent"))

try:
    from zhiwei_agent.tools.lark_tool import LarkTool
    _lark_tool = LarkTool()
    _lark_available = True
except ImportError:
    _lark_available = False
    print("⚠️ LarkTool 未安装，/lark 命令不可用")


def handle_lark_commands(text_lower, text_stripped, user_id, message_id, ctx):
    """
    处理 /lark 命令

    用法：
    - /lark send <群名> <消息>     - 发送消息
    - /lark create-group <群名>    - 创建群聊
    - /lark create-doc <标题>      - 创建文档
    - /lark create-sheet <标题>    - 创建表格
    - /lark create-task <标题>     - 创建任务
    - /lark search <关键词>        - 搜索群聊
    - /lark calendar               - 查看日程
    - /lark help                   - 帮助信息
    """

    # 检查是否可用
    if not _lark_available:
        if text_lower.startswith("/lark"):
            ctx.reply_message(message_id, "⚠️ 飞书功能暂不可用")
            return True
        return False

    # 只处理 /lark 开头的命令
    if not text_lower.startswith("/lark"):
        return False

    # 解析命令
    parts = text_stripped.split(maxsplit=2)
    if len(parts) < 2:
        ctx.reply_message(message_id, _get_help_text())
        return True

    subcommand = parts[1].lower()
    args = parts[2] if len(parts) > 2 else ""

    # 分发处理
    if subcommand == "send":
        return _handle_send(args, user_id, message_id, ctx)
    elif subcommand in ["create-group", "建群"]:
        return _handle_create_group(args, message_id, ctx)
    elif subcommand in ["create-doc", "文档"]:
        return _handle_create_doc(args, message_id, ctx)
    elif subcommand in ["create-sheet", "表格"]:
        return _handle_create_sheet(args, message_id, ctx)
    elif subcommand in ["create-task", "任务"]:
        return _handle_create_task(args, message_id, ctx)
    elif subcommand == "search":
        return _handle_search(args, message_id, ctx)
    elif subcommand in ["calendar", "日程"]:
        return _handle_calendar(message_id, ctx)
    elif subcommand == "help":
        ctx.reply_message(message_id, _get_help_text())
        return True
    else:
        ctx.reply_message(message_id, f"未知命令: {subcommand}\n\n{_get_help_text()}")
        return True


def _handle_send(args, user_id, message_id, ctx):
    """处理发送消息"""
    if not args:
        ctx.reply_message(message_id, "用法: /lark send <群名> <消息内容>")
        return True

    # 解析群名和消息
    parts = args.split(maxsplit=1)
    if len(parts) < 2:
        ctx.reply_message(message_id, "用法: /lark send <群名> <消息内容>")
        return True

    target, text = parts[0], parts[1]

    ctx.reply_message(message_id, f"发送消息到「{target}」...")

    result = _lark_tool.execute(action="send_message", chat_id=target, text=text)

    if result.success:
        ctx.reply_message(message_id, f"✅ 消息已发送到「{target}」")
    else:
        ctx.reply_message(message_id, f"❌ 发送失败: {result.error}")

    return True


def _handle_create_group(args, message_id, ctx):
    """处理创建群聊"""
    if not args:
        ctx.reply_message(message_id, "用法: /lark create-group <群名>")
        return True

    title = args.strip()

    ctx.reply_message(message_id, f"创建群聊「{title}」...")

    result = _lark_tool.execute(action="create_chat", title=title)

    if result.success:
        data = result.data
        msg = f"✅ 已创建群聊「{data.get('name', title)}」"
        if data.get('share_link'):
            msg += f"\n链接: {data.get('share_link')}"
        ctx.reply_message(message_id, msg)
    else:
        ctx.reply_message(message_id, f"❌ 创建失败: {result.error}")

    return True


def _handle_create_doc(args, message_id, ctx):
    """处理创建文档"""
    if not args:
        ctx.reply_message(message_id, "用法: /lark create-doc <标题>")
        return True

    title = args.strip()

    ctx.reply_message(message_id, f"创建文档「{title}」...")

    result = _lark_tool.execute(action="create_doc", title=title)

    if result.success:
        data = result.data
        msg = f"✅ 文档已创建"
        if data.get('doc_url'):
            msg += f"\n链接: {data.get('doc_url')}"
        ctx.reply_message(message_id, msg)
    else:
        ctx.reply_message(message_id, f"❌ 创建失败: {result.error}")

    return True


def _handle_create_sheet(args, message_id, ctx):
    """处理创建表格"""
    if not args:
        ctx.reply_message(message_id, "用法: /lark create-sheet <标题>")
        return True

    title = args.strip()

    ctx.reply_message(message_id, f"创建表格「{title}」...")

    result = _lark_tool.execute(action="create_sheet", title=title)

    if result.success:
        data = result.data
        msg = f"✅ 表格已创建"
        if data.get('url'):
            msg += f"\n链接: {data.get('url')}"
        ctx.reply_message(message_id, msg)
    else:
        ctx.reply_message(message_id, f"❌ 创建失败: {result.error}")

    return True


def _handle_create_task(args, message_id, ctx):
    """处理创建任务"""
    if not args:
        ctx.reply_message(message_id, "用法: /lark create-task <标题>")
        return True

    title = args.strip()

    ctx.reply_message(message_id, f"创建任务「{title}」...")

    result = _lark_tool.execute(action="create_task", title=title)

    if result.success:
        data = result.data
        msg = f"✅ 任务已创建: {data.get('summary', title)}"
        if data.get('url'):
            msg += f"\n链接: {data.get('url')}"
        ctx.reply_message(message_id, msg)
    else:
        ctx.reply_message(message_id, f"❌ 创建失败: {result.error}")

    return True


def _handle_search(args, message_id, ctx):
    """处理搜索群聊"""
    if not args:
        ctx.reply_message(message_id, "用法: /lark search <关键词>")
        return True

    query = args.strip()

    result = _lark_tool.execute(action="search_chat", query=query)

    if result.success:
        chats = result.data.get("chats", [])
        if not chats:
            ctx.reply_message(message_id, f"未找到包含「{query}」的群聊")
        else:
            lines = [f"找到 {len(chats)} 个群聊："]
            for c in chats[:5]:
                lines.append(f"- {c.get('name', '')} (ID: {c.get('chat_id', '')})")
            ctx.reply_message(message_id, "\n".join(lines))
    else:
        ctx.reply_message(message_id, f"❌ 搜索失败: {result.error}")

    return True


def _handle_calendar(message_id, ctx):
    """处理查看日程"""
    result = _lark_tool.execute(action="list_calendar")

    if result.success:
        events = result.data.get("events", [])
        if not events:
            ctx.reply_message(message_id, "近期没有日程安排")
        else:
            lines = [f"近期日程 ({len(events)} 个)："]
            for e in events[:5]:
                lines.append(f"- {e.get('summary', '')}")
            ctx.reply_message(message_id, "\n".join(lines))
    else:
        ctx.reply_message(message_id, f"❌ 查询失败: {result.error}")

    return True


def _get_help_text():
    """获取帮助文本"""
    return """📋 飞书命令帮助

/lark send <群名> <消息>     发送消息
/lark create-group <群名>    创建群聊
/lark create-doc <标题>      创建文档
/lark create-sheet <标题>    创建表格
/lark create-task <标题>     创建任务
/lark search <关键词>        搜索群聊
/lark calendar               查看日程

示例：
  /lark send 知微测试群 大家好
  /lark create-group 项目讨论组
  /lark create-doc 周报"""