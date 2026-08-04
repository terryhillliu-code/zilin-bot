import os
import json
import time
from pathlib import Path
from zhiwei_common.config import ZHIWEI_BOT

# 引入 TTS 状态管理
try:
    from media_handler import tts_enabled_users
except ImportError:
    tts_enabled_users = set()

# 直接导入视频历史管理器（避免 ctx 传递问题）
try:
    from video_history import get_video_history
    _video_history = get_video_history()
except ImportError:
    _video_history = None

def handle_media_commands(text_lower, text_stripped, user_id, message_id, ctx):
    """处理视频链接、语音任务等媒体指令"""
    reply_message = ctx.reply_message

    # 0. TTS 语音回复开关
    if text_lower.startswith("/voice"):
        from media_handler import tts_enabled_users

        parts = text_stripped.split()
        if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
            status = "开启" if user_id in tts_enabled_users else "关闭"
            reply_message(message_id,
                f"🔊 TTS 语音回复状态：{status}\n\n"
                f"发送 `/voice on` 开启语音回复\n"
                f"发送 `/voice off` 关闭语音回复\n\n"
                f"💡 开启后，机器人将用语音回复你的消息"
            )
            return True

        action = parts[1].lower()
        if action == "on":
            tts_enabled_users.add(user_id)
            reply_message(message_id, "✅ 已开启语音回复\n\n🔊 后续消息将使用 Mimo TTS 生成语音")
        else:
            tts_enabled_users.discard(user_id)
            reply_message(message_id, "✅ 已关闭语音回复\n\n后续消息将仅使用文字回复")
        return True

    # 1. 视频链接
    if ctx.is_video_url(text_stripped):
        url = ctx.extract_video_url(text_stripped)

        # 检查重复视频
        if _video_history and url:
            dup = _video_history.check_duplicate(url)
            if dup:
                title = dup.get('title', '未知')[:50]
                processed_at = dup.get('processed_at', '未知')[:10]
                output_path = dup.get('output_path', '')
                output_name = output_path[-50:] if output_path else '未知'

                _pvc = getattr(ctx, 'pending_video_confirm', None)
                if _pvc is not None:
                    _pvc[user_id] = {
                        "url": url, "text": text_stripped,
                        "message_id": message_id, "time": time.time(),
                    }
                reply_message(message_id, f"⚠️ 检测到重复视频\n\n📺 标题: {title}\n📅 处理时间: {processed_at}\n📁 输出文件: ...{output_name}\n\n👉 回复「继续」重新处理，或「取消」放弃")
                return True

        # 回执明确处理模式（2026-08-02：视觉分析已默认开启，告知用户免猜测）
        from media_handler import _wants_vision
        mode_s = "含视觉分析（图表/板书抽帧）" if _wants_vision(text_stripped) else "纯音频（已跳过视觉分析）"
        reply_message(message_id, f"🎬 开始分析视频（{mode_s}）...\n\n⏳ 预计需要3-5分钟，完成后自动回复")
        ctx.handle_video_async(text_stripped, message_id, user_id)
        return True

    # 2. 待办任务自动提取 (关键词触发)
    TODO_KEYWORDS = ["要做的", "要完成", "需要", "得去", "记得", "别忘了", "待办", "记得做", "还要", "要去"]
    if any(kw in text_stripped for kw in TODO_KEYWORDS):
        try:
            bot_dir = Path(str(ZHIWEI_BOT))
            import sys
            if str(bot_dir) not in sys.path:
                sys.path.insert(0, str(bot_dir))
            from voice_task_extractor import extract_tasks
            from voice_task_store import VoiceTaskStore

            tasks = extract_tasks(text_stripped)
            if tasks:
                store = VoiceTaskStore()
                for task in tasks:
                    store.add(content=task["content"], priority=task.get("priority", "normal"), source_text=text_stripped)

                priority_icons = {"high": "🔴", "normal": "🟡", "low": "⚪"}
                task_lines = [f"{priority_icons.get(task.get('priority', 'normal'), '🟡')} {task['content']}" for task in tasks]

                reply_message(message_id, f"📋 已提取 {len(tasks)} 个待办任务\n\n" + "\n".join(task_lines) + "\n\n💡 发送 /任务 查看所有待办")
                if ctx.TaskLogger:
                    ctx.TaskLogger.log_task("待办任务提取", "完成", f"{len(tasks)}个任务")
                return True
        except Exception as e:
            print(f"⚠️ 待办任务提取异常: {e}")
            # 继续正常对话流程

    return False
