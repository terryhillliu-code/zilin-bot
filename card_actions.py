"""飞书卡片交互回调（2026-07-30 从 ws_client.py 拆分）

处理卡片交互事件：审批按钮、v47.0 研究卡片、自然语言路由确认等。
由 ws_client 在 EventDispatcherHandler 中注册。
"""

import json
import os
import time

from feishu_api import reply_message
from utils.model_routing import route_model_for_task

# 使用统一共享包 (v57.0)
from zhiwei_common.message_client import MessageBus


def do_p2_card_action_trigger_v1(data) -> None:
    """处理卡片交互事件 (审批按钮 + v47.0 研究卡片)"""
    try:
        action = data.action
        value = action.value  # 这是一个 dict，包含在卡片定义中
        user_id = data.operator.user_id
        message_id = data.context.open_message_id

        action_type = value.get("action")
        task_id = value.get("task_id")
        plan_name = value.get("plan_name")

        print(f"🔘 卡片交互：{action_type} for {plan_name or task_id} by {user_id}")

        # ⭐ v47.0: 研究卡片回调处理
        if action_type == "start_research":
            topic = value.get("topic", "")
            include_videos = value.get("include_videos", "true").lower() == "true"

            print(f"[WSClient] 研究卡片确认: topic={topic}, videos={include_videos}")

            reply_message(message_id, f"🚀 确认收到调研请求：「{topic}」\n已提交至巡检中心 (zhiwei-dev)，正在排队执行。")

            # 统一入队 zhiwei-dev (backend='research')
            from zhiwei_common import TaskStore
            store = TaskStore()

            research_topic = topic
            if include_videos:
                research_topic += " --include-videos"

            # v33.0: 根据研究主题自动路由模型
            task_model = route_model_for_task(research_topic)
            task_id = store.enqueue(research_topic, message_id=message_id, backend="research", model=task_model)

            # 记录用户映射
            user_mappings_dir = os.path.expanduser("~/zhiwei-dev/user_mappings")
            os.makedirs(user_mappings_dir, exist_ok=True)
            with open(os.path.join(user_mappings_dir, f"task_{task_id}_user.json"), "w") as f:
                json.dump({"user_id": user_id, "message_id": message_id, "source": "feishu"}, f)

            return

        elif action_type == "cancel_research":
            reply_message(message_id, "✅ 已取消研究")
            return

        # ⭐ 2026-07-26 P1: 自然语言路由确认回调（捕获/研究/撤销/取消）
        elif action_type == "confirm_capture":
            from commands.knowledge_commands import do_capture
            from core.confirm_card import build_capture_receipt
            from feishu_api import reply_interactive
            text = value.get("text", "")
            ok, info, filename = do_capture(text, user_id, source="飞书自然语言捕获")
            if ok:
                if not reply_interactive(message_id, build_capture_receipt(filename, info)):
                    reply_message(message_id, f"✅ 已捕获: {filename}")
            else:
                reply_message(message_id, f"❌ 捕获失败: {info}")
            return

        elif action_type == "confirm_research":
            from commands.nl_router import _confirm_research
            from types import SimpleNamespace
            ctx = SimpleNamespace(reply_message=reply_message)
            _confirm_research(value.get("query", ""), user_id, message_id, ctx)
            return

        elif action_type == "undo_capture":
            from pathlib import Path as _Path
            fp = _Path(value.get("filepath", ""))
            try:
                if fp.name.startswith("raw_insight_") and fp.exists():
                    fp.unlink()
                    reply_message(message_id, f"↩️ 已撤销: {fp.name}")
                else:
                    reply_message(message_id, "⚠️ 文件不存在或不可撤销")
            except Exception as e:
                reply_message(message_id, f"❌ 撤销失败: {e}")
            return

        elif action_type == "cancel_nl_action":
            reply_message(message_id, "已取消。")
            return

        elif action_type == "show_config_form":
            # TODO: 显示详细配置表单
            reply_message(message_id, "⚙️ 详细配置功能开发中...")
            return

        # 原有审批逻辑
        if action_type in ["approve", "reject"]:
            # 发送响应消息到 MessageBus
            mb = MessageBus()
            mb.publish(
                sender=f"bot_{user_id}",
                topic="plan_approval",
                content=action_type,
                metadata={
                    "task_id": task_id,
                    "plan_name": plan_name,
                    "user_id": user_id,
                    "timestamp": time.time()
                }
            )

            # 更新卡片状态（可选，这里先简单回复一条消息）
            reply_message(message_id, f"✅ 已收到您的「{ '批准' if action_type == 'approve' else '拒绝' }」操作。正在处理中...")

    except Exception as e:
        print(f"❌ 处理卡片回调失败：{e}")
