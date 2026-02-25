"""
知微 - OpenClaw Agent 飞书机器人
直接桥接 OpenClaw Agent，具备完整能力
"""
import lark_oapi as lark
from lark_oapi.api.im.v1 import *
import json
import re
import subprocess
import os
import time

# 应用凭证
APP_ID = "cli_a9142bd071bd1bd9"
APP_SECRET = "mlIZdNRvxpaVQIB6VQxHIee6WgW4UcPf"

# 消息去重
processed_messages = set()

# 用户会话映射（user_id -> session_id）
user_sessions = {}

# 创建 API 客户端
client = lark.Client.builder() \
    .app_id(APP_ID) \
    .app_secret(APP_SECRET) \
    .build()

def reply_message(message_id: str, text: str):
    """回复消息"""
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
        else:
            print(f"❌ 回复失败: {response.code} - {response.msg}")
            
    except Exception as e:
        print(f"❌ 回复异常: {e}")

def get_session_id(user_id: str, chat_id: str = None) -> str:
    """获取或创建会话 ID"""
    # 私聊用 user_id，群聊用 chat_id + user_id
    if chat_id:
        key = f"{chat_id}:{user_id}"
    else:
        key = user_id
    
    if key not in user_sessions:
        session_id = f"zhiwei-{int(time.time())}-{user_id[-6:]}"
        user_sessions[key] = session_id
    
    return user_sessions[key]

def call_openclaw_agent(message: str, session_id: str, agent: str = "main") -> str:
    """调用 OpenClaw Agent"""
    try:
        cmd = [
            "/usr/local/bin/docker", "exec", "clawdbot",
            "openclaw", "agent",
            "--agent", agent,
            "--message", message,
            "--session-id", session_id,
            "--timeout", "300"
        ]
        
        print(f"🤖 调用 Agent: {agent}, session: {session_id}")
        print(f"   消息: {message[:50]}...")
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300
        )
        
        if result.returncode == 0:
            response = result.stdout.strip()
            print(f"✅ Agent 响应: {len(response)} 字符")
            return response
        else:
            error = result.stderr or result.stdout
            print(f"❌ Agent 错误: {error[:200]}")
            return f"❌ Agent 调用失败:\n{error[:500]}"
            
    except subprocess.TimeoutExpired:
        return "❌ 响应超时（5分钟），请稍后重试"
    except Exception as e:
        print(f"❌ 调用异常: {e}")
        return f"❌ 调用异常: {str(e)}"

def reset_session(user_id: str, chat_id: str = None) -> str:
    """重置会话"""
    if chat_id:
        key = f"{chat_id}:{user_id}"
    else:
        key = user_id
    
    if key in user_sessions:
        del user_sessions[key]
    
    return "✅ 会话已重置，开始新对话"

def show_help() -> str:
    """显示帮助"""
    return """🤖 知微 - OpenClaw Agent

我具备完整的 AI 能力：
✅ 多轮对话（记住上下文）
✅ 文件读写、命令执行
✅ 代码分析、报告生成
✅ 调用各种工具和 Skills

命令：
• /help - 显示帮助
• /reset - 重置对话（清除上下文）
• /session - 查看当前会话 ID
• m1-m8 - 切换模型

模型：
1-Qwen3.5  2-Coder  3-Max  4-Kimi  
5-GLM5  6-MiniMax  7-Plus  8-Max按量

💡 直接发消息开始对话！"""

def process_message(text: str, user_id: str, chat_id: str = None) -> str:
    """处理消息"""
    text_lower = text.strip().lower()
    
    # 特殊命令
    if text_lower in ["/help", "帮助", "/帮助"]:
        return show_help()
    
    if text_lower in ["/reset", "重置", "/重置", "新对话"]:
        return reset_session(user_id, chat_id)
    
    if text_lower in ["/session", "会话"]:
        session_id = get_session_id(user_id, chat_id)
        return f"📌 当前会话: {session_id}"
    
    # 模型切换（m1-m8）
    if len(text_lower) == 2 and text_lower[0] == 'm' and text_lower[1] in "12345678":
        # 调用宿主机的 ocmodel 命令
        import subprocess
        try:
            result = subprocess.run(
                ['/usr/local/bin/ocmodel', text_lower[1]],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                return f"✅ {result.stdout}"
            else:
                return f"❌ 切换失败: {result.stderr}"
        except Exception as e:
            return f"❌ 切换异常: {e}"
    
    # 获取会话 ID
    session_id = get_session_id(user_id, chat_id)
    
    # 调用 OpenClaw Agent
    return call_openclaw_agent(text, session_id)

def do_p2_im_message_receive_v1(data) -> None:
    """处理收到的消息"""
    global processed_messages
    
    try:
        event = data.event
        message = event.message
        message_id = message.message_id
        
        # 去重
        if message_id in processed_messages:
            return
        processed_messages.add(message_id)
        
        # 保持集合大小
        if len(processed_messages) > 1000:
            processed_messages = set(list(processed_messages)[-500:])
        
        # 获取消息信息
        chat_type = message.chat_type
        msg_type = message.message_type
        content_str = message.content
        chat_id = message.chat_id if hasattr(message, 'chat_id') else None
        
        # 获取发送者
        sender = event.sender
        if sender and sender.sender_id and sender.sender_id.user_id:
            user_id = sender.sender_id.user_id
        else:
            user_id = "unknown"
        
        print(f"\n{'='*50}")
        print(f"📨 [{chat_type}] 用户: {user_id[:10] if len(user_id) >= 10 else user_id}...")
        
        if msg_type != "text":
            print("   ⏭️ 非文本消息，跳过")
            return
        
        content_dict = json.loads(content_str)
        text = content_dict.get("text", "")
        
        # 移除 @ 提及
        text = re.sub(r'@_user_\d+\s*', '', text).strip()
        
        print(f"   消息: {text[:50]}...")
        print(f"{'='*50}")
        
        if text:
            response = process_message(text, user_id, chat_id)
            reply_message(message_id, response)
        
    except Exception as e:
        print(f"❌ 处理错误: {e}")
        import traceback
        traceback.print_exc()

def main():
    event_handler = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(do_p2_im_message_receive_v1) \
        .build()
    
    cli = lark.ws.Client(
        APP_ID,
        APP_SECRET,
        event_handler=event_handler,
        log_level=lark.LogLevel.INFO
    )
    
    print("🤖 知微 Agent 启动")
    print("   命令: /help | /reset")
    print("   直接发消息调用 OpenClaw Agent")
    print("-" * 50)
    
    cli.start()

if __name__ == "__main__":
    main()
