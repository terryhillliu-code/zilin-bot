"""
三层记忆系统
- Layer 1: 工作记忆（最近N轮完整对话）
- Layer 2: 摘要记忆（旧对话压缩为摘要）
- Layer 3: 持久记忆（任务记录、用户偏好）
"""
import json
import os
import time
import httpx
from datetime import datetime


class MemoryManager:
    def __init__(self, user_id, max_working_rounds=6):
        self.user_id = user_id
        self.max_working_rounds = max_working_rounds
        self.memory_dir = os.path.expanduser("~/logs/memory")
        os.makedirs(self.memory_dir, exist_ok=True)

        self.state_file = os.path.join(self.memory_dir, f"{user_id}_state.json")
        self.persistent_file = os.path.join(self.memory_dir, f"{user_id}_persistent.json")

        self.working_memory = []
        self.summary = ""
        self._load_state()

    def add_turn(self, user_msg, assistant_msg):
        self.working_memory.append({
            "user": user_msg[:500],
            "assistant": assistant_msg[:500],
            "time": datetime.now().isoformat()
        })
        if len(self.working_memory) > self.max_working_rounds:
            self._compress_oldest()
        self._save_state()

    def build_context_prompt(self) -> str:
        parts = []
        if self.summary:
            parts.append(f"[历史摘要] {self.summary}")
        if self.working_memory:
            recent = []
            for turn in self.working_memory[-4:]:
                recent.append(f"用户: {turn['user'][:200]}")
                recent.append(f"助手: {turn['assistant'][:200]}")
            parts.append("[最近对话]\n" + "\n".join(recent))
        persistent = self._load_persistent()
        if persistent:
            items = []
            for k, v in list(persistent.items())[-5:]:
                items.append(f"- {k}: {v['value']}")
            if items:
                parts.append("[用户偏好]\n" + "\n".join(items))
        if not parts:
            return ""
        return "\n\n".join(parts)

    def reset(self):
        self.working_memory = []
        self.summary = ""
        self._save_state()

    def _compress_oldest(self):
        if len(self.working_memory) <= self.max_working_rounds:
            return
        old_turns = self.working_memory[:2]
        self.working_memory = self.working_memory[2:]
        old_text = ""
        for turn in old_turns:
            old_text += f"用户: {turn['user'][:150]}\n助手: {turn['assistant'][:150]}\n"
        new_summary = self._call_compress_llm(old_text)
        if new_summary:
            self.summary = new_summary

    def _call_compress_llm(self, old_text) -> str:
        try:
            api_key = self._get_api_key()
            if not api_key:
                return self._simple_compress(old_text)
            prompt = f"""请将以下对话历史压缩为简洁摘要，保留关键信息：

之前的摘要：{self.summary or '无'}

新的对话：
{old_text}

要求：只保留关键事实决策结论，控制在150字以内"""

            response = httpx.post(
                "https://coding.dashscope.aliyuncs.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": "qwen3.5-plus", "messages": [{"role": "user", "content": prompt}], "max_tokens": 300},
                timeout=15
            )
            if response.status_code == 200:
                result = response.json()["choices"][0]["message"]["content"]
                print(f"🧠 摘要压缩完成: {len(result)} 字符")
                return result[:300]
            else:
                return self._simple_compress(old_text)
        except Exception as e:
            print(f"⚠️ 摘要压缩异常: {e}")
            return self._simple_compress(old_text)

    def _simple_compress(self, old_text) -> str:
        existing = self.summary or ""
        combined = existing + " | " + old_text.replace("\n", " ")
        return combined[:300]

    def save_persistent(self, key, value):
        data = self._load_persistent()
        data[key] = {"value": value, "time": datetime.now().isoformat()}
        if len(data) > 50:
            sorted_items = sorted(data.items(), key=lambda x: x[1].get("time", ""))
            data = dict(sorted_items[-50:])
        with open(self.persistent_file, 'w') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def get_persistent(self, key=None):
        data = self._load_persistent()
        if key:
            return data.get(key, {}).get("value")
        return data

    def _load_persistent(self) -> dict:
        if os.path.exists(self.persistent_file):
            try:
                with open(self.persistent_file, 'r') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                return {}  # 持久化记忆加载失败，返回空字典
        return {}

    def _save_state(self):
        state = {"working_memory": self.working_memory, "summary": self.summary, "updated": datetime.now().isoformat()}
        try:
            with open(self.state_file, 'w') as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"⚠️ 记忆保存失败: {e}")

    def _load_state(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r') as f:
                    state = json.load(f)
                self.working_memory = state.get("working_memory", [])
                self.summary = state.get("summary", "")
                print(f"🧠 恢复记忆: {len(self.working_memory)} 轮对话")
            except (json.JSONDecodeError, IOError):
                self.working_memory = []  # 状态加载失败，重置为空
                self.summary = ""

    def _get_api_key(self) -> str:
        """获取 API Key - 支持多路径和多变量名查找"""
        env_paths = [
            os.path.expanduser("~/clawdbot-docker/workspace/secrets/.env"),
            os.path.expanduser("~/zhiwei-bot/.env"),
            os.path.expanduser("~/tanwei-bot/.env"),
        ]
        key_names = ["CODING_PLAN_API_KEY", "DASHSCOPE_API_KEY", "BAILIAN_API_KEY"]

        for env_path in env_paths:
            if os.path.exists(env_path):
                with open(env_path) as f:
                    lines = f.readlines()
                for key_name in key_names:
                    for line in lines:
                        if line.startswith(f"{key_name}="):
                            return line.split("=", 1)[1].strip().strip('"\'')
        return None

    def get_stats(self) -> str:
        persistent_count = len(self._load_persistent())
        return f"工作记忆: {len(self.working_memory)}/{self.max_working_rounds} 轮\n摘要: {len(self.summary)} 字符\n持久记忆: {persistent_count} 条"


def extract_important_info(user_msg: str, assistant_msg: str) -> dict:
    """
    从对话中提取重要信息（简单规则匹配）
    返回: {"key": "...", "value": "..."} 或 None
    """
    import re
    
    combined = f"{user_msg} {assistant_msg}".lower()
    
    # 偏好类
    if any(kw in combined for kw in ["我喜欢", "我偏好", "我习惯", "我常用"]):
        # 提取偏好
        match = re.search(r"(我喜欢|我偏好|我习惯|我常用)(.{2,30})", combined)
        if match:
            return {"key": "用户偏好", "value": match.group(2).strip()}
    
    # 任务完成类
    if any(kw in assistant_msg.lower() for kw in ["已完成", "已创建", "已部署", "已修复"]):
        return {"key": "完成任务", "value": assistant_msg[:100]}
    
    # 决策类
    if any(kw in combined for kw in ["决定", "选择", "采用", "使用"]):
        match = re.search(r"(决定|选择|采用|使用)(.{2,50})", combined)
        if match:
            return {"key": "决策记录", "value": match.group(2).strip()[:50]}
    
    return None
