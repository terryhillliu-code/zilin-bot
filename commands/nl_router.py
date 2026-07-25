"""自然语言路由器（2026-07-26 P1）

插在命令链末端、ChatHandler 兜底之前：斜杠命令不受影响，非命令文本先经一轮
LLM 意图识别再分派。任何异常返回 False → ChatHandler 兜底，严格不差于现状。

确认策略：capture 置信度 ≥0.85 直存 + 撤销回执；0.6-0.85 卡片/关键词确认；
<0.6 反问。research 总是确认。关键词确认与卡片按钮双通道（按钮回调在 ws_client）。
"""
import json
import re

from zhiwei_common.llm import llm_client

INTENT_PROMPT = """你是飞书机器人「知微」的意图识别层。根据用户消息判断意图，只输出 JSON，不要解释。

意图类型:
- knowledge_query: 查询/查找/回忆知识库中的内容（笔记、论文、以前记过的资料）
- capture: 要求记录/保存/记下某条信息、灵感或想法
- research: 要求对某主题做深入调研/研究/整理报告
- status: 询问机器人或系统自身的状态/健康
- chat: 闲聊、问候或不属于以上的任何内容

输出格式（严格 JSON）:
{"intent": "...", "confidence": 0.0到1.0的数, "topic": "核心主题或内容摘要（capture 意图放原始要点内容）", "action_summary": "一句话描述将执行的动作"}

示例:
用户: 知识库里关于 HBM4 的笔记有哪些？
{"intent": "knowledge_query", "confidence": 0.95, "topic": "HBM4", "action_summary": "检索知识库中 HBM4 相关笔记"}
用户: 记一下：MLA 架构用光互连做长上下文缓存这个思路值得跟
{"intent": "capture", "confidence": 0.92, "topic": "MLA 架构用光互连做长上下文缓存的思路值得跟进", "action_summary": "保存该想法到知识库"}
用户: 帮我深入研究一下 CXL 内存池化
{"intent": "research", "confidence": 0.9, "topic": "CXL 内存池化", "action_summary": "对 CXL 内存池化做扩展检索与整理"}
用户: 早上好
{"intent": "chat", "confidence": 0.95, "topic": null, "action_summary": "闲聊"}"""

# 确认策略阈值
CAPTURE_AUTO = 0.85    # ≥ 直存 + 撤销回执
CAPTURE_CONFIRM = 0.6  # [CONFIRM, AUTO) 卡片/关键词确认；< 反问

# 待确认状态：user_id -> {"kind": "capture"|"research", "content": str}
_PENDING = {}

_CONFIRM_WORDS = {"确认", "存", "是", "好", "好的", "要", "可以", "ok", "yes", "y"}
_CANCEL_WORDS = {"取消", "不", "不要", "算了", "no", "n"}


def _parse_intent(text):
    """单轮 LLM 意图识别 → dict；失败返回 None"""
    try:
        success, response = llm_client.call("chat", text, system_prompt=INTENT_PROMPT, timeout=15)
        if not success or not response:
            return None
        m = re.search(r"(\{.*\})", response, re.DOTALL)
        if not m:
            return None
        data = json.loads(m.group(1))
        if data.get("intent") not in ("knowledge_query", "capture", "research", "status", "chat"):
            return None
        data["confidence"] = float(data.get("confidence", 0.5))
        return data
    except Exception:
        return None


def _confirm_research(query, user_id, message_id, ctx):
    from commands.knowledge_commands import do_knowledge_query
    do_knowledge_query(query, user_id, message_id, ctx, deep=True)


def route_natural_language(text, user_id, message_id, ctx) -> bool:
    """自然语言主路由。True=已消费；False=交回 ChatHandler"""
    if not text or len(text.strip()) < 2:
        return False

    try:
        stripped = text.strip()
        low = stripped.lower()

        # 0) 待确认动作的关键词通道（与卡片按钮双通道）
        pending = _PENDING.get(user_id)
        if pending and low in _CONFIRM_WORDS:
            _PENDING.pop(user_id, None)
            if pending["kind"] == "research":
                _confirm_research(pending["content"], user_id, message_id, ctx)
            else:
                from commands.knowledge_commands import do_capture
                ok, info, filename = do_capture(pending["content"], user_id, source="飞书自然语言捕获")
                ctx.reply_message(message_id, f"✅ 已捕获: {filename}" if ok else f"❌ 捕获失败: {info}")
            return True
        if pending and low in _CANCEL_WORDS:
            _PENDING.pop(user_id, None)
            ctx.reply_message(message_id, "已取消。")
            return True

        # 1) 意图识别
        intent = _parse_intent(stripped)
        if not intent:
            return False
        kind = intent["intent"]
        conf = intent["confidence"]
        topic = intent.get("topic") or ""
        summary = intent.get("action_summary") or ""

        # 2) 知识问答：直达
        if kind == "knowledge_query":
            from commands.knowledge_commands import do_knowledge_query
            do_knowledge_query(topic or stripped, user_id, message_id, ctx)
            return True

        # 3) 捕获：按置信度分档
        if kind == "capture":
            content = topic or stripped
            if conf >= CAPTURE_AUTO:
                from commands.knowledge_commands import do_capture
                ok, info, filename = do_capture(content, user_id, source="飞书自然语言捕获")
                if ok:
                    from core.confirm_card import build_capture_receipt
                    from feishu_api import reply_interactive
                    if not reply_interactive(message_id, build_capture_receipt(filename, info)):
                        ctx.reply_message(message_id, f"✅ 已捕获: {filename}（卡片发送失败，文件已存）")
                else:
                    ctx.reply_message(message_id, f"❌ 捕获失败: {info}")
                return True
            # 中低置信度：登记待确认 + 卡片（卡片失败降级为纯文本提问）
            _PENDING[user_id] = {"kind": "capture", "content": content}
            if conf >= CAPTURE_CONFIRM:
                from core.confirm_card import build_confirmation
                from feishu_api import reply_interactive
                card = build_confirmation(
                    "📝 存入知识库？",
                    f"**内容**: {content[:200]}\n\n🎯 置信度 {int(conf * 100)}% · {summary}",
                    "confirm_capture", {"text": content})
                if reply_interactive(message_id, card):
                    return True
            ctx.reply_message(message_id,
                              f"要把下面这条存入知识库吗？回复「确认」存入：\n\n{content[:200]}")
            return True

        # 4) 研究：总是确认
        if kind == "research":
            query = topic or stripped
            _PENDING[user_id] = {"kind": "research", "content": query}
            from core.confirm_card import build_confirmation
            from feishu_api import reply_interactive
            card = build_confirmation(
                "🔬 开始深度研究？",
                f"**主题**: {query}\n\n将执行扩展知识库检索 + 整理回答 · {summary}",
                "confirm_research", {"query": query}, confirm_label="✅ 开始")
            if reply_interactive(message_id, card):
                return True
            ctx.reply_message(message_id, f"要对「{query}」做深度研究吗？回复「确认」开始。")
            return True

        # 5) 系统状态：复用 /status
        if kind == "status":
            from commands.system_commands import handle_system_commands
            return handle_system_commands("/status", "/status", user_id, message_id, ctx)

        # 6) chat 等 → 交回 ChatHandler
        return False
    except Exception:
        return False
