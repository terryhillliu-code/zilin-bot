import requests
import json
import time

class OpenClawClient:
    def __init__(self, base_url="http://localhost:18789", token="aoK2fs4tmv7c-cLj0_wiBLwwcHUaF1mV9BOWUWw7uD4"):
        self.base_url = base_url
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

    def call_agent(self, agent_name, message, session_id=None, timeout=300):
        """通过 HTTP API 调用 OpenClaw Agent"""
        url = f"{self.base_url}/v1/chat/completions"
        
        # 构造 OpenAI 兼容格式的消息
        payload = {
            "model": agent_name,  # OpenClaw 通常将 agent 映射为 model 名
            "messages": [{"role": "user", "content": message}],
            "user": session_id or "default_user",
            "stream": False
        }

        try:
            start_time = time.time()
            response = requests.post(url, headers=self.headers, json=payload, timeout=timeout)
            duration = time.time() - start_time
            
            if response.status_code == 200:
                result = response.json()
                content = result['choices'][0]['message']['content']
                print(f"✅ API 调用成功 (耗时: {duration:.2f}s)")
                return content
            else:
                print(f"❌ API 错误: {response.status_code} - {response.text}")
                return f"❌ AI 接口异常: {response.status_code}"
                
        except Exception as e:
            print(f"⚠️ API 请求异常: {str(e)}")
            return f"⚠️ 无法连接到 AI 大脑"

# 单测代码
if __name__ == "__main__":
    client = OpenClawClient()
    print("📡 正在测试 API 连接...")
    print(client.call_agent("main", "ping"))
