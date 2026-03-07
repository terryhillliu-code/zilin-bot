"""
飞书 API 消息回复模块
提供 reply_message 和 reply_card 函数
"""

import lark_oapi as lark
import json
import time
from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody, CreateMessageRequest, CreateMessageRequestBody

# 导入全局 client（在 ws_client.py 中定义）
# 通过 from ws_client import client 的方式引入
client = None

# 飞书 API 配额管理
from feishu_quota import record_call


def init_feishu_api(global_client):
    """初始化飞书 API 模块，传入全局 client"""
    global client
    client = global_client


def reply_message(message_id: str, text: str) -> bool:
    """回复文本消息，失败自动重试（共3次尝试）"""
    max_retries = 3
    retry_delays = [0, 1, 3]  # 首次立即，第二次等1秒，第三次等3秒

    for attempt in range(max_retries):
        if attempt > 0:
            print(f"⏳ reply_message 重试 {attempt}/{max_retries-1}，等待 {retry_delays[attempt]}s...")
            time.sleep(retry_delays[attempt])

        try:
            if len(text) > 4000:
                text = text[:3900] + "\n\n...(内容过长已截断)"
            content = json.dumps({"text": text})
            request = ReplyMessageRequest.builder() \
                .message_id(message_id) \
                .request_body(ReplyMessageRequestBody.builder()
                    .content(content)
                    .msg_type("text")
                    .build()) \
                .build()
            response = client.im.v1.message.reply(request)
            if response.success():
                print(f"✅ 回复成功 ({len(text)} 字符)")
                record_call("reply")  # 记录 API 调用
                return True
            else:
                print(f"❌ 回复失败: {response.code} - {response.msg}")
                # 飞书业务错误不重试（如无效的 message_id）
                return False
        except Exception as e:
            print(f"❌ 回复异常 (attempt {attempt+1}): {e}")
            # 网络异常继续重试

    print(f"❌ reply_message 最终失败，已重试 {max_retries-1} 次")
    return False


def send_direct_message(user_id: str, text: str) -> bool:
    """通过用户ID直接发送消息，用于主动推送"""
    max_retries = 3
    retry_delays = [0, 1, 3]  # 首次立即，第二次等1秒，第三次等3秒

    for attempt in range(max_retries):
        if attempt > 0:
            print(f"⏳ send_direct_message 重试 {attempt}/{max_retries-1}，等待 {retry_delays[attempt]}s...")
            time.sleep(retry_delays[attempt])

        try:
            if len(text) > 4000:
                text = text[:3900] + "\n\n...(内容过长已截断)"
            content = json.dumps({"text": text})

            request = CreateMessageRequest.builder() \
                .receive_id_type("open_id") \
                .request_body(CreateMessageRequestBody.builder()
                    .receive_id(user_id)
                    .content(content)
                    .msg_type("text")
                    .build()) \
                .build()

            response = client.im.v1.message.create(request)
            if response.success():
                print(f"✅ 主动推送成功 ({len(text)} 字符) to user {user_id[:8]}...")
                record_call("send")  # 记录 API 调用
                return True
            else:
                print(f"❌ 主动推送失败: {response.code} - {response.msg}")
                print(f"   错误详情: {response.error or '无详情'}")
                # 飞书业务错误不重试（如无效的 user_id）
                return False
        except Exception as e:
            print(f"❌ 主动推送异常 (attempt {attempt+1}): {e}")
            import traceback
            traceback.print_exc()
            # 网络异常继续重试

    print(f"❌ send_direct_message 最终失败，已重试 {max_retries-1} 次")
    return False


def reply_card(message_id: str, title: str, content_text: str):
    """回复卡片消息"""
    try:
        if len(content_text) > 3500:
            content_text = content_text[:3400] + "\n\n...(内容过长已截断)"

        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "blue"
            },
            "elements": [
                {"tag": "markdown", "content": content_text}
            ]
        }

        request = ReplyMessageRequest.builder() \
            .message_id(message_id) \
            .request_body(ReplyMessageRequestBody.builder()
                .content(json.dumps(card))
                .msg_type("interactive")
                .build()) \
            .build()

        response = client.im.v1.message.reply(request)
        if response.success():
            print(f"✅ 卡片回复成功")
            record_call("reply")  # 记录 API 调用
        else:
            print(f"⚠️ 卡片失败，回退文本: {response.code}")
            reply_message(message_id, f"{title}\n\n{content_text}")
    except Exception as e:
        print(f"❌ 卡片异常: {e}")
        reply_message(message_id, f"{title}\n\n{content_text}")
