import os
import json
from pathlib import Path
from zhiwei_common.config import ZHIWEI_BOT

def handle_media_commands(text_lower, text_stripped, user_id, message_id, ctx):
    """处理视频链接、语音任务等媒体指令"""
    reply_message = ctx.reply_message
    
    # 1. 视频链接
    if ctx.is_video_url(text_stripped):
        video_history = ctx.get_video_history()
        url = ctx.extract_video_url(text_stripped)
        
        if video_history and url:
            dup = video_history.check_duplicate(url)
            if dup:
                if ctx.pending_video_confirm is not None:
                    ctx.pending_video_confirm[user_id] = {
                        "url": url,
                        "history": dup,
                        "text": text_stripped,
                        "message_id": message_id
                    }
                
                title = dup.get('title', '未知')[:50]
                processed_at = dup.get('processed_at', '未知')[:10]
                output_path = dup.get('output_path', '')
                output_name = output_path[-50:]
                
                reply_message(message_id, f"⚠️ 检测到重复视频\n\n📺 标题: {title}\n📅 处理时间: {processed_at}\n📁 输出文件: ...{output_name}\n\n👉 回复「继续」重新处理，或「取消」放弃")
                return True
                
        reply_message(message_id, "🎬 开始分析视频...\n\n⏳ 预计需要3-5分钟，完成后自动回复")
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
