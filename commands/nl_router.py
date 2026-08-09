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
- learn_concept: 想系统性学习/了解某个技术概念（原理、演进、横向对比），生成概念卡片沉淀进知识图谱。触发词如"学一下/我想了解/给我讲讲/梳理一下X"。注意与 knowledge_query 的区别：learn_concept 是"教我学懂这个概念"，knowledge_query 是"查我库里已有的资料"
- capture: 要求记录/保存/记下某条信息、灵感或想法
- research: 要求对某主题做深入调研/研究/整理报告（一次性报告，不沉淀概念卡片）
- image_gen: 要求生成/画/绘制一张图片（文生图）。触发词如"帮我画一张…/生成一张…图片/画个…"。注意与图片分析严格区分："分析/描述/看看这张图"是对已有图片的理解，不是 image_gen；image_gen 必须是"从无到有创造图片"的请求
- status: 询问机器人或系统自身的状态/健康
- media_followup: 针对「刚才发的视频/文章/播客」的追问、修正或要求重做。
  识别标志：出现"刚才/那个视频/这条链接/重新分析/代入/你没理解"等回指词，且消息中不含新链接。
  action 取 qa（基于已有产物直接问答）或 reanalyze（用户给了新指令/映射，要求重跑管线）。
- chat: 闲聊、问候或不属于以上的任何内容

输出格式（严格 JSON）:
{"intent": "...", "confidence": 0.0到1.0的数, "topic": "核心主题或内容摘要（capture 意图放原始要点内容）", "action_summary": "一句话描述将执行的动作",
 "action": "qa|reanalyze（仅 media_followup 输出）", "instruction": "用户的完整指令原文（仅 media_followup 输出）"}
（非 media_followup 意图可不输出 action/instruction，向后兼容）

示例:
用户: 知识库里关于 HBM4 的笔记有哪些？
{"intent": "knowledge_query", "confidence": 0.95, "topic": "HBM4", "action_summary": "检索知识库中 HBM4 相关笔记"}
用户: 记一下：MLA 架构用光互连做长上下文缓存这个思路值得跟
{"intent": "capture", "confidence": 0.92, "topic": "MLA 架构用光互连做长上下文缓存的思路值得跟进", "action_summary": "保存该想法到知识库"}
用户: 帮我深入研究一下 CXL 内存池化
{"intent": "research", "confidence": 0.9, "topic": "CXL 内存池化", "action_summary": "对 CXL 内存池化做扩展检索与整理"}
用户: 学一下 Muon 这个优化器
{"intent": "learn_concept", "confidence": 0.95, "topic": "Muon", "action_summary": "生成 Muon 概念学习卡片并接入知识图谱"}
用户: 我想系统了解一下牛顿-舒尔茨迭代
{"intent": "learn_concept", "confidence": 0.9, "topic": "牛顿-舒尔茨迭代", "action_summary": "生成 牛顿-舒尔茨迭代 概念学习卡片"}
用户: 帮我画一张在月光下读书的猫
{"intent": "image_gen", "confidence": 0.95, "topic": "在月光下读书的猫", "action_summary": "用 FLUX 生成一张图片"}
用户: 生成一张赛博朋克城市夜景的图片
{"intent": "image_gen", "confidence": 0.95, "topic": "赛博朋克城市夜景", "action_summary": "用 FLUX 生成一张图片"}
用户: 帮我分析这张图片里讲了什么
{"intent": "chat", "confidence": 0.6, "topic": "图片内容分析", "action_summary": "分析已有图片（非生图）"}
用户: 早上好
{"intent": "chat", "confidence": 0.95, "topic": null, "action_summary": "闲聊"}
用户: 这个博主的狮驼岭是指美国，凤仙郡是指中国，请代入重新分析刚才那个视频
{"intent": "media_followup", "confidence": 0.92, "action": "reanalyze", "instruction": "狮驼岭=美国、凤仙郡=中国、棒子=韩国、鬼子=日本。代入这些真实指向还原代称后，重新输出整个视频的观点和摘要", "action_summary": "带代称映射重跑视频分析"}
用户: 刚才那个视频第三点再展开讲讲
{"intent": "media_followup", "confidence": 0.9, "action": "qa", "instruction": "展开讲第三点", "action_summary": "基于最近视频笔记回答追问"}"""

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
        # ⭐ 2026-08-04: 意图识别 → classify 任务 (qwen3-coder-next, 0.8s)
        success, response = llm_client.call_by_task("classify", text, system_prompt=INTENT_PROMPT, timeout=15)
        if not success or not response:
            return None
        m = re.search(r"(\{.*\})", response, re.DOTALL)
        if not m:
            return None
        data = json.loads(m.group(1))
        if data.get("intent") not in ("knowledge_query", "capture", "research", "status", "chat", "learn_concept", "media_followup", "image_gen"):
            return None
        data["confidence"] = float(data.get("confidence", 0.5))
        # media_followup 的 action 仅允许 qa/reanalyze，非法值置 None
        if data.get("intent") == "media_followup" and data.get("action") not in ("qa", "reanalyze"):
            data["action"] = None
        return data
    except Exception:
        return None


def _confirm_research(query, user_id, message_id, ctx):
    from commands.knowledge_commands import do_knowledge_query
    do_knowledge_query(query, user_id, message_id, ctx, deep=True)


# ⭐ 2026-08-05: 代称映射 + 重析 的关键词预筛（单一真相；command_handler 也复用本函数）
_MAP_KW = ["代称", "代入", "带入", "映射", "指的是", "是指", "代指"]
_REDO_KW = ["重新分析", "再分析", "重新输出", "重新整理", "重做", "重新处理", "再看一遍"]
_REF_KW = ["刚才", "那个视频", "这条链接", "那个文章", "那条", "他的那个"]


def is_remap_reanalyze(text: str) -> bool:
    """是否为「给代称做映射并重新分析刚才那个媒体」类请求"""
    return (any(k in text for k in _MAP_KW)
            and any(k in text for k in _REDO_KW)
            and any(k in text for k in _REF_KW))


def _exec_media_followup(action, artifact, instruction, user_id, message_id, ctx) -> bool:
    """执行媒体追问：qa 基于产物笔记回答，reanalyze 带用户指令重跑管线（2026-08-04 P1.2）"""
    from core.conversation_store import conversation_store
    if action == "reanalyze":
        conversation_store.set_instruction(artifact["id"], instruction)
        try:
            from media_handler import reprocess_with_instruction  # P1.3 提供
            reprocess_with_instruction(user_id, artifact, instruction, message_id)
            ctx.reply_message(message_id, "🎬 已带你的指令重新分析，约 3-5 分钟出结果。")
        except ImportError:
            ctx.reply_message(message_id, "📝 已记录你的指令，重跑管线能力即将上线，暂请重发链接处理。")
        except Exception as e:
            ctx.reply_message(message_id, f"❌ 重跑失败: {e}")
        return True
    # qa：基于产物笔记回答
    note = ""
    np = artifact.get("note_path")
    if np:
        try:
            from pathlib import Path
            note = Path(np).read_text(errors="ignore")[:4000]
        except Exception:
            note = ""
    if not note:
        note = artifact.get("summary") or ""
    msg = (f"【背景：{artifact.get('kind', '内容')}《{artifact.get('title', '')}》的笔记】\n{note}\n\n"
           f"【用户追问】{instruction}")
    ans = llm_client.call_by_task_with_session("context_qa", msg, f"feishu-{user_id}")
    if ans and not ans.startswith("❌ AI 暂时无法响应"):
        ctx.reply_message(message_id, ans)
        conversation_store.record_turn(user_id, "user", instruction)
        conversation_store.record_turn(user_id, "assistant", ans)
    else:
        ctx.reply_message(message_id, ans or "❌ 追问回答失败，请稍后重试。")
    return True


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
            elif pending["kind"] == "learn":
                from commands.learn_commands import do_learn
                do_learn(pending["content"], user_id, message_id, ctx)
            elif pending["kind"] == "image_gen":
                from commands.image_commands import handle_image_gen_async
                handle_image_gen_async(pending["content"], user_id, message_id, ctx)
            elif pending["kind"] == "media_followup":
                c = pending["content"]
                from core.conversation_store import conversation_store
                artifact = conversation_store.get_last_artifact(user_id)
                if not artifact or not isinstance(c, dict):
                    ctx.reply_message(message_id, "原产物已失效，请重发链接重新处理。")
                else:
                    _exec_media_followup(c.get("action", "qa"), artifact,
                                         c.get("instruction", ""), user_id, message_id, ctx)
            else:
                from commands.knowledge_commands import do_capture
                ok, info, filename = do_capture(pending["content"], user_id, source="飞书自然语言捕获")
                ctx.reply_message(message_id, f"✅ 已捕获: {filename}" if ok else f"❌ 捕获失败: {info}")
            return True
        if pending and low in _CANCEL_WORDS:
            _PENDING.pop(user_id, None)
            ctx.reply_message(message_id, "已取消。")
            return True

        # 0.5) 关键词预筛：代称映射 + 重析 → 直接命中 media_followup-reanalyze
        # （2026-08-05: 8/4 狮驼岭场景实测 LLM 分类会把这种复杂句式误判为 RAG 检索）
        if is_remap_reanalyze(stripped):
            from core.conversation_store import conversation_store
            artifact = conversation_store.get_last_artifact(user_id)
            if artifact:
                return _exec_media_followup("reanalyze", artifact, stripped, user_id, message_id, ctx)
            ctx.reply_message(message_id, "我这边没有找到最近处理过的视频/文章，请把链接重发一次，我重新处理。")
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

        # 2.4) 生图：高置信直达（GPU 异步生成），中置信确认，低置信放行
        if kind == "image_gen":
            gen_prompt = topic or stripped
            if conf >= 0.75:
                from commands.image_commands import handle_image_gen_async
                handle_image_gen_async(gen_prompt, user_id, message_id, ctx)
                return True
            if conf >= 0.5:
                _PENDING[user_id] = {"kind": "image_gen", "content": gen_prompt}
                ctx.reply_message(message_id,
                                  f"要为「{gen_prompt[:60]}」生成一张图片吗？（GPU 出图约 2-4 分钟）回复「确认」开始。")
                return True
            return False

        # 2.5) 概念学习：高置信直达（~60s 生成概念卡），中置信确认，低置信放行
        if kind == "learn_concept":
            concept = topic or stripped
            if conf >= 0.75:
                from commands.learn_commands import do_learn
                do_learn(concept, user_id, message_id, ctx)
                return True
            if conf >= 0.5:
                _PENDING[user_id] = {"kind": "learn", "content": concept}
                ctx.reply_message(message_id,
                                  f"要为「{concept}」生成概念学习卡片吗？（约 1 分钟，沉淀进知识图谱）回复「确认」开始。")
                return True
            return False

        # 2.6) 媒体追问：基于最近产物问答或带指令重析（2026-08-04 P1.2）
        if kind == "media_followup":
            if conf < 0.5:
                return False
            from core.conversation_store import conversation_store
            artifact = conversation_store.get_last_artifact(user_id)
            if not artifact:
                ctx.reply_message(message_id,
                    "我这边没有找到最近处理过的视频/文章，请把链接重发一次，我重新处理。")
                return True
            instruction = intent.get("instruction") or topic or stripped
            action = intent.get("action") or "qa"
            if conf < 0.75:
                _PENDING[user_id] = {"kind": "media_followup",
                                     "content": {"action": action, "instruction": instruction,
                                                 "artifact_id": artifact["id"]}}
                _kind_zh = "视频" if artifact["kind"] == "video" else "内容"
                _act_zh = "按你的指令重新分析" if action == "reanalyze" else "回答你的追问"
                ctx.reply_message(message_id,
                    f"理解为：基于最近的{_kind_zh}《{artifact.get('title', '')[:30]}》，"
                    f"{_act_zh}。回复「确认」执行。")
                return True
            return _exec_media_followup(action, artifact, instruction, user_id, message_id, ctx)

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
                # ⭐ 2026-08-05: 混合意图处理 — capture 后检查「另外」后的追问部分
                _SEP = ["另外", "还有", "同时", "顺便"]
                for sep in _SEP:
                    if sep in stripped:
                        remaining = stripped.split(sep, 1)[-1].strip()
                        if remaining and len(remaining) > 4:
                            # 递归处理剩余部分（最多一次，避免无限循环）
                            _recur_key = f"_mixed_{user_id}"
                            if not getattr(route_natural_language, _recur_key, False):
                                setattr(route_natural_language, _recur_key, True)
                                try:
                                    route_natural_language(remaining, user_id, message_id, ctx)
                                finally:
                                    setattr(route_natural_language, _recur_key, False)
                        break
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

        # 5) 系统状态：直接本地取（/status 的探微重定向已失效）
        if kind == "status":
            from command_handler import get_quick_status
            ctx.reply_message(message_id, get_quick_status())
            return True

        # 6) chat 等 → 交回 ChatHandler
        return False
    except Exception:
        return False
