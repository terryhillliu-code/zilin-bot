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


def reply_message(message_id: str, text: str, **kwargs) -> bool:
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


def reply_interactive(message_id: str, card: dict, **kwargs) -> bool:
    """回复交互卡片（msg_type=interactive，2026-07-26 P1）。失败返回 False，调用方降级为纯文本"""
    try:
        request = ReplyMessageRequest.builder() \
            .message_id(message_id) \
            .request_body(ReplyMessageRequestBody.builder()
                .content(json.dumps(card, ensure_ascii=False))
                .msg_type("interactive")
                .build()) \
            .build()
        response = client.im.v1.message.reply(request)
        if response.success():
            record_call("reply")
            return True
        print(f"❌ 卡片回复失败: {response.code} - {response.msg}")
        return False
    except Exception as e:
        print(f"❌ 卡片回复异常: {e}")
        return False


def send_direct_message(user_id: str, text: str, **kwargs) -> bool:
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

            # 自动识别 ID 类型
            id_type = kwargs.get("receive_id_type")
            if not id_type:
                if user_id.startswith("ou_"):
                    id_type = "open_id"
                elif user_id.startswith("on_"):
                    id_type = "union_id"
                elif user_id.startswith("oc_"):
                    id_type = "chat_id"
                else:
                    id_type = "user_id"

            request = CreateMessageRequest.builder() \
                .receive_id_type(id_type) \
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


def send_audio_reply(message_id: str, audio_file_path: str) -> bool:
    """发送语音消息回复

    流程：
    1. 上传音频文件到飞书，获取 file_key
    2. 发送 msg_type="audio" 的回复消息

    Args:
        message_id: 原始消息 ID
        audio_file_path: 音频文件路径（mp3 或 opus 格式）

    Returns:
        是否发送成功
    """
    try:
        # 1. 上传文件到飞书
        from lark_oapi.api.im.v1 import UploadAllFileRequest, UploadAllFileRequestBody

        with open(audio_file_path, "rb") as f:
            file_content = f.read()

        request = UploadAllFileRequest.builder() \
            .request_body(UploadAllFileRequestBody.builder()
                .file_type("opus")  # 飞书语音消息使用 opus 格式
                .file_name("tts_reply.opus")
                .content(file_content)
                .parent_type("message")
                .parent_id(message_id)
                .build()) \
            .build()

        response = client.im.v1.file.upload_all(request)

        if not response.success():
            print(f"❌ 音频上传失败: {response.code} - {response.msg}")
            return False

        file_key = response.file_key
        if not file_key:
            print("❌ 音频上传成功但未返回 file_key")
            return False

        print(f"✅ 音频上传成功: file_key={file_key}")
        record_call("upload_audio")

        # 2. 发送语音消息
        content = json.dumps({"file_key": file_key})
        reply_request = ReplyMessageRequest.builder() \
            .message_id(message_id) \
            .request_body(ReplyMessageRequestBody.builder()
                .content(content)
                .msg_type("audio")
                .build()) \
            .build()

        reply_response = client.im.v1.message.reply(reply_request)

        if reply_response.success():
            print("✅ 语音消息发送成功")
            record_call("reply_audio")
            return True
        else:
            print(f"❌ 语音消息发送失败: {reply_response.code} - {reply_response.msg}")
            return False

    except Exception as e:
        print(f"❌ 发送语音消息异常: {e}")
        import traceback
        traceback.print_exc()
        return False
