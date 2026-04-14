import os
import json
import subprocess
import traceback
from zhiwei_common.task_client import TaskStore
from zhiwei_common.config import ZHIWEI_SCHEDULER, ZHIWEI_DEV
from utils.model_routing import route_model_for_task

def handle_dev_commands(text_lower, text_stripped, user_id, message_id, ctx):
    """处理 /dev, /accept, /reject 等开发工作流命令"""
    pending_review = ctx.pending_review
    reply_message = ctx.reply_message

    # 1. 审批确认流程 (T-056)
    if user_id in pending_review:
        task_id = pending_review[user_id]
        store = TaskStore()
        task = store.get(task_id)

        if task and task.get("status") == "awaiting_review":
            if text_lower in ["好", "可以", "执行", "ok", "yes", "同意", "批准", "行", "执行吧", "没问题", "approve", "确认"]:
                branch = task.get("branch")
                repo_path = task.get("repo_path") or str(ZHIWEI_SCHEDULER)
                merge_result = subprocess.run(["git", "merge", branch, "--no-edit"],
                    cwd=repo_path, capture_output=True, text=True)

                if merge_result.returncode == 0:
                    store.accept(task_id)
                    del pending_review[user_id]
                    reply_message(message_id, f"✅ 任务 #{task_id} 已确认完成并合并")
                else:
                    reply_message(message_id, f"❌ 合并失败:\n{merge_result.stderr[:500]}")
                return True

            elif text_lower in ["不要", "取消", "不", "no", "拒绝", "算了", "不行", "reject", "重做"]:
                reason = text_stripped.split(maxsplit=1)[1] if " " in text_stripped else "用户拒绝"
                store.reject_with_retry(task_id, reason)
                del pending_review[user_id]
                reply_message(message_id, f"🔄 任务 #{task_id} 已拒绝，将重新执行\n\n原因: {reason}")
                return True

    # 2. 开发任务显式触发 (T-052)
    if text_lower.startswith("/dev ") or text_lower.startswith("@开发 "):
        requirement = text_stripped.split(" ", 1)[1] if " " in text_stripped else ""
        if not requirement:
            reply_message(message_id, "❌ 请提供需求描述\n\n用法: /dev 把早报时间改成8点30分")
            return True

        try:
            store = TaskStore()
            
            # v32.4 优化核心逻辑评估风险
            risk_level = "approve" 
            if any(kw in requirement.lower() for kw in ["分析", "查看", "检查", "搜"]):
                risk_level = "auto"
            
            initial_status = "pending" if risk_level == "auto" else "review"
            # v33.0: 根据需求文本自动路由模型
            task_model = route_model_for_task(requirement)
            task_id = store.enqueue(requirement, message_id=message_id, initial_status=initial_status, model=task_model)
            daily_seq = store.get_daily_seq(task_id)

            # 记录用户映射
            user_mappings_dir = os.path.join(str(ZHIWEI_DEV), "user_mappings")
            os.makedirs(user_mappings_dir, exist_ok=True)
            with open(os.path.join(user_mappings_dir, f"task_{task_id}_user.json"), "w") as f:
                json.dump({"user_id": user_id, "message_id": message_id, "source": "feishu"}, f)

            if initial_status == "review":
                pending_review[user_id] = task_id
                reply_message(message_id, f"👋 收到开发需求\n\n📌 {requirement}\n\n🕒 风险判定: {risk_level}\n👉 请回复「批准」执行，或回复「取消」放弃")
            else:
                reply_message(message_id, f"👋 收到开发需求\n✅ 风险判定[安全]，直接加入队列\n\n任务 日常#{daily_seq} 已排队，请耐心等待~")
            
            return True
        except Exception as e:
            traceback.print_exc()
            reply_message(message_id, f"❌ 投递任务失败: {e}")
            return True

    # 3. /status 命令 (由系统指令处理)
    return False
