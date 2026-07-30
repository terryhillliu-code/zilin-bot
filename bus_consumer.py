"""MessageBus 消费线程（2026-07-30 从 ws_client.py 拆分）

轮询 messages.db 的 feishu_notification / feishu_card_notification / notification
主题，并推送到飞书：
- 文本通知走 feishu_api.send_direct_message
- 卡片通知走 im.v1 message.create（自动识别 receive_id 类型）

依赖注入：client / APP_ID / last_active_user / load_active_user 由 ws_client
调用 start_bus_consumer 时传入，避免循环 import。
"""

import json
import threading
import time
from datetime import datetime

from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
)

# 使用统一共享包 (v57.0)
from zhiwei_common.message_client import MessageBus


def start_bus_consumer(client, APP_ID, last_active_user, load_active_user):
    """启动统一的 MessageBus 消费线程，返回线程对象"""

    def poll_message_bus():
        import feishu_api as _feishu_api_mod
        mb = MessageBus()
        print("💡 MessageBus 消费线程已启动")

        # 启动时发布自检消息（带冷却期检查）
        try:
            COOLDOWN_MINUTES = 30  # 30 分钟内不重复发送
            with mb._connect() as conn:
                cursor = conn.execute(
                    """SELECT created_at FROM messages
                       WHERE sender = 'bot/startup' AND topic = 'feishu_notification'
                       ORDER BY created_at DESC LIMIT 1"""
                )
                row = cursor.fetchone()
                if row:
                    from datetime import timedelta
                    last_startup = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
                    if datetime.now() - last_startup < timedelta(minutes=COOLDOWN_MINUTES):
                        print(f"⏸️ 跳过启动通知（{COOLDOWN_MINUTES}分钟冷却期内，上次: {row[0]}）")
                    else:
                        raise Exception("cooldown expired")  # 跳转到 publish
                else:
                    raise Exception("no previous startup")
        except:
            try:
                mb.publish(
                    sender="bot/startup",
                    topic="feishu_notification",
                    content=f"🤖知微机器人已重启\n时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\nAppID: {APP_ID[:8]}...",
                    metadata={"user_id": load_active_user()}
                )
            except: pass

        while True:
            try:
                # 消费飞书通知和审批主题 (v55.0: 包含 legacy notification)
                topics = ["feishu_notification", "feishu_card_notification", "notification"]
                for topic in topics:
                    try:
                        messages = mb.consume_pending(topic=topic, limit=5)
                        if messages:
                            print(f"📥 MessageBus: 发现 {len(messages)} 条新消息 ({topic})")
                        for msg in messages:
                            print(f"🔄 MessageBus: 正在处理消息 {msg['id']}...")
                            try:
                                meta = json.loads(msg["metadata"] or "{}")
                                target_user = meta.get("user_id") or last_active_user.get("user_id") or load_active_user()

                                if not target_user:
                                    print(f"⚠️ MessageBus: 消息 {msg['id']} 找不到目标用户，标记为失败")
                                    mb.mark_failed(msg["id"], "No target user found")
                                    continue

                                if topic in ["feishu_notification", "notification"]:
                                    success = _feishu_api_mod.send_direct_message(target_user, msg["content"])
                                else:
                                    # 卡片消息 - 自动识别 ID 类型
                                    id_type = "user_id"
                                    if target_user.startswith("ou_"):
                                        id_type = "open_id"
                                    elif target_user.startswith("on_"):
                                        id_type = "union_id"
                                    elif target_user.startswith("oc_"):
                                        id_type = "chat_id"

                                    request = CreateMessageRequest.builder() \
                                        .receive_id_type(id_type) \
                                        .request_body(CreateMessageRequestBody.builder()
                                            .receive_id(target_user)
                                            .content(msg["content"])
                                            .msg_type("interactive")
                                            .build()) \
                                        .build()
                                    response = client.im.v1.message.create(request)
                                    success = response.success()

                                if success:
                                    mb.mark_sent(msg["id"])
                                    print(f"✅ MessageBus: 消息 {msg['id']} 已成功推送到飞书")
                                else:
                                    mb.mark_failed(msg["id"], "Feishu API delivery failed")
                                    print(f"❌ MessageBus: 消息 {msg['id']} 推送失败")
                            except Exception as em:
                                print(f"❌ MessageBus: 处理单条消息 {msg['id']} 异常：{em}")
                                mb.mark_failed(msg["id"], str(em))
                    except Exception as et:
                        print(f"❌ MessageBus: [Topic: {topic}] 轮询异常：{et}")
                time.sleep(2)
            except Exception as e:
                print(f"❌ MessageBus: 主循环异常：{e}")
                time.sleep(5)

    msg_bus_thread = threading.Thread(target=poll_message_bus, daemon=True)
    msg_bus_thread.start()
    return msg_bus_thread
