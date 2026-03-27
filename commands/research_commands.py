import os
import json
from zhiwei_common.task_client import TaskStore

def handle_research_commands(text_lower, text_stripped, user_id, message_id, ctx):
    """处理 /notebooklm, /report, /research 等研究任务命令"""
    
    if text_lower.startswith("/notebooklm ") or text_lower.startswith("/report ") or text_lower.startswith("/research "):
        try:
            parts = text_stripped.split(maxsplit=1)
            if len(parts) < 2:
                ctx.reply_message(message_id, "❌ 请提供关键词，例如: `/research 混合专家模型`")
                return True
                
            topic = parts[1]
            ctx.reply_message(message_id, f"🚀 收到研究项目：{topic}\n\n已加入任务仓库 (zhiwei-dev)，正在排队执行。您可以发送 /status 查看进度。")
            
            store = TaskStore()
            task_id = store.enqueue(topic, message_id=message_id, backend="research")
            
            # 记录用户映射
            user_mappings_dir = os.path.expanduser("~/zhiwei-dev/user_mappings")
            os.makedirs(user_mappings_dir, exist_ok=True)
            with open(os.path.join(user_mappings_dir, f"task_{task_id}_user.json"), "w") as f:
                json.dump({"user_id": user_id, "message_id": message_id, "source": "feishu"}, f)
            
            return True
        except Exception as e:
            ctx.reply_message(message_id, f"❌ 投递任务失败: {e}")
            return True

    return False
