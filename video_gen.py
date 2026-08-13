"""飞书驱动 H3 视频生成：草稿-确认-执行三段式（2026-08-09）

链路：nl_router video_gen 意图 → 本模块分流：
  主题清晰 → LLM 六块模板扩写 → 草稿卡（确认/修改/取消）
  主题缺失 → 引导提问 → 用户补充后出草稿
确认后 → detached video_gen_runner.py → gpu_offload.submit("h3") → 成片直链交付。

门控原则：LLM 只提议不决策；推断块显式标注；不点确认不占 GPU。
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from zhiwei_common.llm import llm_client

STATE_FILE = Path.home() / "zhiwei-bot" / "state" / "video_gen_pending.json"
TEMPLATE_FILE = Path.home() / "zhiwei-bot" / "templates" / "h3_prompt_template.md"
RUNNER_SCRIPT = Path.home() / "zhiwei-bot" / "scripts" / "video_gen_runner.py"
PENDING_TTL = 7200  # pending 状态 2 小时过期

# 本地档位（实测基线）
WIDTH, HEIGHT, STEPS = 1152, 640, 20
VALID_DURATIONS = (5, 8, 10)
DEFAULT_DURATION = 5

# 触发词剥离后剩余内容过短 → 判定主题缺失
_TRIGGER_WORDS = ["帮我", "帮忙", "请", "给我", "生成", "做个", "做一段", "做一条",
                  "来一段", "来一个", "制作", "创作", "视频", "片子", "短片", "一段"]

EXPAND_SYSTEM_PROMPT = """你是 MiniMax H3 视频生成模型的提示词编剧。把用户的简单需求扩写成结构化提示词。

{template}

输出要求（严格 JSON，不要解释）：
{{
  "prompt": "最终完整提示词（中文，按六块结构组织：风格契约→时间线分镜→摄像机→音频→文字拼写(如需要)→否定列表，时间线用 [0s-Xs] 标记）",
  "duration": 5 或 8 或 10（根据内容复杂度选，默认 5）,
  "inferred": ["timeline", "audio", "camera", "style", "negative"] 中你推断补全的块名列表（用户原文已明确的不要列入）,
  "style": "风格预设名（电影写实/复古动漫/纪录片/广告质感/自定义）"
}}

规则：
- 用户已给出的内容必须原样保留，只补全缺失块
- 时间线分镜段数约为 duration/2
- 音频块必须包含具体声音与进入时间点
- 否定列表至少包含：不要软溶解、不要字幕、不要水印
- prompt 总长控制在 300-600 字"""

GUIDED_QUESTION = (
    "🎬 想帮你生成视频，还需要一点信息：\n\n"
    "**1. 拍什么？**（主体 + 场景，例如：黄昏海边的灯塔）\n"
    "**2. 什么风格？**（电影写实 / 复古动漫 / 纪录片 / 广告质感，或自己描述）\n"
    "**3. 多长？**（5 / 8 / 10 秒）\n\n"
    "直接回复即可，一句话也行，比如：「雪山日出，电影写实，8 秒」\n"
    "（生成草稿后你还可以修改，确认后才开始生成，约 25 分钟）")


# ========== pending 状态管理 ==========

def _load_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def _get_pending(user_id: str) -> dict | None:
    state = _load_state()
    rec = state.get(user_id)
    if not rec:
        return None
    if time.time() - rec.get("ts", 0) > PENDING_TTL:
        state.pop(user_id, None)
        _save_state(state)
        return None
    return rec


def _set_pending(user_id: str, rec: dict):
    state = _load_state()
    # 顺手清理过期项
    now = time.time()
    state = {k: v for k, v in state.items() if now - v.get("ts", 0) <= PENDING_TTL}
    rec["ts"] = now
    state[user_id] = rec
    _save_state(state)


def _clear_pending(user_id: str):
    state = _load_state()
    if user_id in state:
        state.pop(user_id, None)
        _save_state(state)


# ========== 分流入口 ==========

def _strip_trigger(text: str) -> str:
    t = text
    for w in _TRIGGER_WORDS:
        t = t.replace(w, "")
    return re.sub(r"\s+", " ", t).strip(" ，。,.:：")


def handle_video_gen_request(request_text: str, user_id: str, message_id: str, ctx):
    """nl_router video_gen 意图入口"""
    core = _strip_trigger(request_text)
    if len(core) < 6:
        # 主题缺失 → 引导提问
        _set_pending(user_id, {"stage": "awaiting_input", "request": request_text})
        ctx.reply_message(message_id, GUIDED_QUESTION)
        return
    _make_draft_and_send(request_text, user_id, message_id, ctx)


_CONFIRM_WORDS = {"确认", "确认生成", "确定", "好", "好的", "可以", "开始", "ok", "yes", "y"}
_CANCEL_WORDS = {"取消", "算了", "不要了", "no", "n"}


def consume_user_input(text: str, user_id: str, message_id: str, ctx) -> bool:
    """pending 状态消费（与卡片按钮双通道，防按钮回调失联）。返回 True=已消费

    awaiting_input：用户补充内容 → 合并出草稿
    awaiting_confirm：确认词→执行；取消词→取消；其他文本→视为修改意见合并重出草稿
    """
    rec = _get_pending(user_id)
    if not rec:
        return False
    stage = rec.get("stage")
    low = text.strip().lower()
    if stage == "awaiting_confirm":
        if low in _CONFIRM_WORDS:
            action_confirm(user_id, message_id, ctx)
            return True
        if low in _CANCEL_WORDS:
            action_cancel(user_id, message_id, ctx)
            return True
        # 其他文本 → 视为修改意见：合并进原需求重新扩写
        prev_req = rec.get("request", "")
        merged = f"{prev_req}；修改意见：{text}"
        _make_draft_and_send(merged, user_id, message_id, ctx)
        return True
    if stage == "awaiting_input":
        prev = rec.get("request", "")
        merged = text if len(_strip_trigger(prev)) < 6 else f"{prev}；{text}"
        _make_draft_and_send(merged, user_id, message_id, ctx)
        return True
    return False


# ========== 草稿生成 ==========

def _load_template() -> str:
    try:
        return TEMPLATE_FILE.read_text(encoding="utf-8")
    except Exception:
        return ""


def _build_draft(request_text: str) -> dict | None:
    """LLM 六块扩写 → dict(prompt/duration/inferred/style)；失败 None"""
    tpl = _load_template()
    system = EXPAND_SYSTEM_PROMPT.format(template=tpl)
    msg = f"用户需求：{request_text}\n\n请扩写并输出 JSON。"
    try:
        success, response = llm_client.call_by_task(
            "structured", msg, system_prompt=system, timeout=90)
        if not success or not response:
            return None
        m = re.search(r"(\{.*\})", response, re.DOTALL)
        if not m:
            return None
        data = json.loads(m.group(1))
        if not data.get("prompt"):
            return None
        dur = data.get("duration", DEFAULT_DURATION)
        try:
            dur = int(dur)
        except Exception:
            dur = DEFAULT_DURATION
        data["duration"] = dur if dur in VALID_DURATIONS else DEFAULT_DURATION
        data["inferred"] = [b for b in (data.get("inferred") or [])
                            if isinstance(b, str)]
        return data
    except Exception as e:
        print(f"❌ H3 草稿扩写失败: {e}")
        return None


_BLOCK_NAMES = {
    "style": "风格契约", "timeline": "时间线", "camera": "摄像机",
    "audio": "音频", "text": "文字拼写", "negative": "否定列表",
}


def _make_draft_and_send(request_text: str, user_id: str, message_id: str, ctx):
    ctx.reply_message(message_id, "🎬 正在按 H3 六块结构扩写提示词（约 10 秒）...")
    draft = _build_draft(request_text)
    if not draft:
        ctx.reply_message(message_id, "❌ 草稿生成失败（LLM 暂不可用），请稍后重试。")
        return
    _set_pending(user_id, {
        "stage": "awaiting_confirm",
        "request": request_text,
        "draft": draft,
    })
    if not _send_draft_card(user_id, message_id, draft, ctx):
        # 卡片失败降级纯文本
        ctx.reply_message(
            message_id,
            f"📝 H3 视频草稿（{draft['duration']} 秒）\n\n{draft['prompt']}\n\n"
            f"回复「确认」开始生成（约 25 分钟），回复「取消」放弃。")


def _send_draft_card(user_id: str, message_id: str, draft: dict, ctx) -> bool:
    inferred = [_BLOCK_NAMES.get(b, b) for b in draft.get("inferred", [])]
    inferred_note = ("、".join(inferred) + " 为推断补全，可修改") if inferred else "全部来自你的描述"
    md = (
        f"**时长** {draft['duration']} 秒 ｜ **分辨率** {WIDTH}×{HEIGHT} ｜ "
        f"**风格** {draft.get('style', '自定义')}\n"
        f"**预计耗时** 约 25 分钟\n"
        f"〔{inferred_note}〕\n\n"
        f"---\n\n{draft['prompt']}"
    )
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "🎬 H3 视频生成草稿（待确认）"},
            "template": "purple",
        },
        "elements": [
            {"tag": "markdown", "content": md},
            {"tag": "action", "actions": [
                {"tag": "button", "type": "primary",
                 "text": {"tag": "plain_text", "content": "✅ 确认生成"},
                 "value": {"action": "video_gen_confirm", "uid": user_id}},
                {"tag": "button",
                 "text": {"tag": "plain_text", "content": "✏️ 修改"},
                 "value": {"action": "video_gen_modify", "uid": user_id}},
                {"tag": "button", "type": "danger",
                 "text": {"tag": "plain_text", "content": "取消"},
                 "value": {"action": "video_gen_cancel", "uid": user_id}},
            ]},
        ],
    }
    try:
        from feishu_api import reply_interactive
        return reply_interactive(message_id, card)
    except Exception as e:
        print(f"❌ 草稿卡片发送失败: {e}")
        return False


# ========== 卡片动作 ==========

def action_confirm(user_id: str, message_id: str, ctx):
    rec = _get_pending(user_id)
    if not rec or rec.get("stage") != "awaiting_confirm":
        ctx.reply_message(message_id, "⚠️ 没有待确认的视频草稿（可能已过期，请重新发起）。")
        return
    draft = rec["draft"]
    payload = {
        "user_id": user_id,
        "prompt": draft["prompt"],
        "duration": draft["duration"],
        "width": WIDTH, "height": HEIGHT, "steps": STEPS,
        "request": rec.get("request", ""),
    }
    payload_path = f"/tmp/video_gen_payload_{int(time.time())}.json"
    Path(payload_path).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    try:
        subprocess.Popen(
            [sys.executable, str(RUNNER_SCRIPT), payload_path],
            start_new_session=True,
            stdout=open("/dev/null", "w"), stderr=subprocess.STDOUT)
    except Exception as e:
        ctx.reply_message(message_id, f"❌ 任务启动失败: {e}")
        return
    rec["stage"] = "generating"
    _set_pending(user_id, rec)
    ctx.reply_message(
        message_id,
        f"🚀 已提交 H3 视频生成（{draft['duration']} 秒 / {WIDTH}×{HEIGHT}）\n"
        f"⏳ 预计约 25 分钟，完成后自动发送成片直链。\n"
        f"（笔记本离线或 GPU 忙时会通知失败原因）")


def action_modify(user_id: str, message_id: str, ctx):
    rec = _get_pending(user_id)
    if not rec or rec.get("stage") != "awaiting_confirm":
        ctx.reply_message(message_id, "⚠️ 没有可修改的草稿（可能已过期）。")
        return
    rec["stage"] = "awaiting_input"
    _set_pending(user_id, rec)
    ctx.reply_message(
        message_id,
        "✏️ 请发送修改内容：\n"
        "- 完整的新提示词 → 直接替换草稿\n"
        "- 修改意见（如「改成赛博朋克风格」「时长 8 秒」）→ 我会合并进原草稿重新出稿")


def action_cancel(user_id: str, message_id: str, ctx):
    _clear_pending(user_id)
    ctx.reply_message(message_id, "已取消视频生成。")
