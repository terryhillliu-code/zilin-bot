"""
三层记忆系统 + 向量语义检索
- Layer 1: 工作记忆（最近N轮完整对话）
- Layer 2: 摘要记忆（旧对话压缩为摘要）
- Layer 3: 持久记忆（任务记录、用户偏好）
- Layer 4: 向量记忆（语义检索历史对话）
"""
import json
import os
import time
import uuid
from datetime import datetime
from typing import Optional, List
from dataclasses import dataclass

# LanceDB 向量存储
try:
    import lancedb
    import pyarrow as pa
    HAS_LANCEDB = True
except ImportError:
    HAS_LANCEDB = False
    print("⚠️ LanceDB 未安装，向量检索功能不可用")

# Embedding 服务
_EMBED_SERVICE_URL = "http://127.0.0.1:8765/embed"


def call_embed_service(texts: list[str]) -> Optional[list[list[float]]]:
    """调用常驻 Embedding 服务获取向量"""
    try:
        import requests
        resp = requests.post(_EMBED_SERVICE_URL, json={"texts": texts}, timeout=30)
        if resp.status_code == 200:
            return resp.json().get("embeddings", [])
    except Exception as e:
        print(f"⚠️ Embedding 服务调用失败: {e}")
    return None


@dataclass
class MemoryVector:
    """记忆向量存储结构"""
    id: str
    user_id: str
    text: str
    user_msg: str
    assistant_msg: str
    memory_type: str  # preference / task / decision / conversation
    timestamp: str
    vector: list = None

    def __post_init__(self):
        if self.vector is None:
            self.vector = []


class MemoryVectorStore:
    """记忆向量存储（基于 LanceDB）"""

    TABLE_NAME = "memory_vectors"

    def __init__(self, db_path: str = "~/logs/memory/vector_db"):
        if not HAS_LANCEDB:
            raise ImportError("需要安装 lancedb 和 pyarrow")

        self.db_path = os.path.expanduser(db_path)
        os.makedirs(self.db_path, exist_ok=True)
        self.db = lancedb.connect(self.db_path)
        self._table = None

    @property
    def table(self):
        if self._table is not None:
            return self._table
        if self.TABLE_NAME in self.db.table_names():
            self._table = self.db.open_table(self.TABLE_NAME)
        return self._table

    def create_table(self, dimension: int = 1024):
        """创建向量表"""
        if self.TABLE_NAME in self.db.table_names():
            self._table = self.db.open_table(self.TABLE_NAME)
            return

        schema = pa.schema([
            pa.field("id", pa.string()),
            pa.field("user_id", pa.string()),
            pa.field("text", pa.string()),
            pa.field("user_msg", pa.string()),
            pa.field("assistant_msg", pa.string()),
            pa.field("memory_type", pa.string()),
            pa.field("timestamp", pa.string()),
            pa.field("vector", pa.list_(pa.float32(), dimension)),
        ])
        self._table = self.db.create_table(self.TABLE_NAME, schema=schema, mode="overwrite")
        print(f"🧠 创建记忆向量表: {self.TABLE_NAME}")

    def add_memory(self, memory: MemoryVector):
        """添加单条记忆向量"""
        if self.table is None:
            self.create_table()

        # 获取向量（检查 None 或空列表）
        if not memory.vector or len(memory.vector) == 0:
            embeddings = call_embed_service([memory.text])
            if embeddings and len(embeddings) > 0:
                memory.vector = embeddings[0]
            else:
                print(f"⚠️ 无法获取向量，跳过记忆: {memory.id}")
                return

        import pyarrow as pa
        record = {
            "id": memory.id,
            "user_id": memory.user_id,
            "text": memory.text,
            "user_msg": memory.user_msg,
            "assistant_msg": memory.assistant_msg,
            "memory_type": memory.memory_type,
            "timestamp": memory.timestamp,
            "vector": memory.vector,
        }
        self.table.add([record])

    def search(self, query_text: str, user_id: str, top_k: int = 5) -> List[dict]:
        """语义搜索相关记忆"""
        if self.table is None:
            return []

        embeddings = call_embed_service([query_text])
        if not embeddings or len(embeddings) == 0:
            return []

        query_vector = embeddings[0]
        safe_user_id = self._validate_user_id(user_id)
        results = self.table.search(query_vector).where(f"user_id = '{safe_user_id}'").limit(top_k).to_list()
        return results

    def count(self, user_id: str = None) -> int:
        """记忆数量（优化版：不加载全部数据）"""
        if self.table is None:
            return 0
        if user_id:
            # 使用小 limit 快速估算，避免加载大量数据
            safe_user_id = self._validate_user_id(user_id)
            try:
                # 尝试获取精确计数（部分 LanceDB 版本支持）
                return self.table.count_rows(filter=f"user_id = '{safe_user_id}'")
            except TypeError:
                # 降级：使用 limit 估算，但只加载最多 100 条用于判断是否有数据
                sample = self.table.search().where(f"user_id = '{safe_user_id}'").limit(100).to_list()
                if len(sample) < 100:
                    return len(sample)
                # 有超过 100 条，返回估算值
                return len(sample)  # 实际应用中很少需要精确计数
        return self.table.count_rows()

    def _validate_user_id(self, user_id: str) -> str:
        """验证并转义 user_id"""
        # 只允许字母、数字、下划线、横线
        if not all(c.isalnum() or c in '-_' for c in user_id):
            raise ValueError(f"Invalid user_id format: {user_id}")
        return user_id.replace("'", "''")


class MemoryManager:
    def __init__(self, user_id, max_working_rounds=6, enable_vector=True):
        self.user_id = user_id
        self.max_working_rounds = max_working_rounds
        self.memory_dir = os.path.expanduser("~/logs/memory")
        os.makedirs(self.memory_dir, exist_ok=True)

        self.state_file = os.path.join(self.memory_dir, f"{user_id}_state.json")
        self.persistent_file = os.path.join(self.memory_dir, f"{user_id}_persistent.json")

        self.working_memory = []
        self.summary = ""
        self._load_state()

        # 初始化向量存储
        self.vector_store = None
        if enable_vector and HAS_LANCEDB:
            try:
                self.vector_store = MemoryVectorStore()
                print(f"🧠 向量记忆系统已启用")
            except Exception as e:
                print(f"⚠️ 向量存储初始化失败: {e}")

    def add_turn(self, user_msg, assistant_msg):
        self.working_memory.append({
            "user": user_msg[:500],
            "assistant": assistant_msg[:500],
            "time": datetime.now().isoformat()
        })
        if len(self.working_memory) > self.max_working_rounds:
            self._compress_oldest()
        self._save_state()

        # 提取重要信息并存储向量
        if self.vector_store:
            extracted = extract_important_info_enhanced(user_msg, assistant_msg)
            if extracted:
                self._store_memory_vector(extracted, user_msg, assistant_msg)

    def build_context_prompt(self, current_query: str = None) -> str:
        parts = []

        # Layer 1: 工作记忆（最近对话）
        if self.summary:
            parts.append(f"[历史摘要] {self.summary}")
        if self.working_memory:
            recent = []
            for turn in self.working_memory[-4:]:
                recent.append(f"用户: {turn['user'][:200]}")
                recent.append(f"助手: {turn['assistant'][:200]}")
            parts.append("[最近对话]\n" + "\n".join(recent))

        # Layer 2: 语义搜索相关记忆
        if current_query and self.vector_store:
            relevant = self.search_relevant_memory(current_query)
            if relevant:
                parts.append("[相关历史]\n" + relevant)

        # Layer 3: 持久记忆（偏好）
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

    def search_relevant_memory(self, query: str, top_k: int = 3) -> str:
        """语义搜索相关记忆"""
        if not self.vector_store:
            return ""

        results = self.vector_store.search(query, self.user_id, top_k)
        if not results:
            return ""

        lines = []
        for r in results[:top_k]:
            memory_type = r.get("memory_type", "conversation")
            text = r.get("text", "")[:150]
            lines.append(f"[{memory_type}] {text}")

        return "\n".join(lines)

    def _store_memory_vector(self, extracted: dict, user_msg: str, assistant_msg: str):
        """存储记忆向量"""
        if not self.vector_store:
            return

        memory = MemoryVector(
            id=str(uuid.uuid4()),
            user_id=self.user_id,
            text=extracted.get("value", ""),
            user_msg=user_msg[:300],
            assistant_msg=assistant_msg[:300],
            memory_type=extracted.get("key", "conversation"),
            timestamp=datetime.now().isoformat(),
        )
        self.vector_store.add_memory(memory)

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
            # 使用统一 LLM 出口
            try:
                from llm_client import llm_client
            except ImportError:
                return self._simple_compress(old_text)

            prompt = f"""请将以下对话历史压缩为简洁摘要，保留关键信息：

之前的摘要：{self.summary or '无'}

新的对话：
{old_text}

要求：只保留关键事实决策结论，控制在150字以内"""

            success, result = llm_client.call(
                role="format",
                message=prompt,
                timeout=15,
            )
            if success and result:
                print(f"🧠 摘要压缩完成: {len(result)} 字符")
                return result[:300]
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


def extract_important_info_enhanced(user_msg: str, assistant_msg: str) -> dict:
    """
    增强版记忆提取（规则 + LLM 智能提取）
    返回: {"key": "...", "value": "..."} 或 None
    """
    import re

    combined = f"{user_msg} {assistant_msg}"
    combined_lower = combined.lower()

    # Phase 1: 规则提取（快速匹配）

    # 偏好类（扩展关键词）
    preference_keywords = [
        "我喜欢", "我偏好", "我习惯", "我常用", "我一般",
        "我不喜欢", "我讨厌", "我希望", "我的习惯", "我倾向于"
    ]
    if any(kw in combined_lower for kw in preference_keywords):
        for kw in preference_keywords:
            if kw in combined_lower:
                match = re.search(rf"{kw}(.{{2,50}})", combined)
                if match:
                    return {"key": "用户偏好", "value": match.group(1).strip()[:80]}

    # 任务完成类（扩展关键词）
    task_keywords = [
        "已完成", "已创建", "已部署", "已修复", "已完成",
        "任务完成", "执行完成", "成功", "已实现"
    ]
    if any(kw in assistant_msg.lower() for kw in task_keywords):
        # 提取任务描述
        for kw in task_keywords:
            if kw in assistant_msg.lower():
                match = re.search(rf"(.{{0,30}}){kw}", assistant_msg)
                if match:
                    task_desc = match.group(1).strip() if match.group(1) else kw
                    return {"key": "完成任务", "value": f"{task_desc}: {kw}"}
        return {"key": "完成任务", "value": assistant_msg[:100]}

    # 决策类（扩展关键词）
    decision_keywords = [
        "决定", "选择", "采用", "使用", "方案", "策略",
        "配置", "设置", "参数", "规划"
    ]
    if any(kw in combined_lower for kw in decision_keywords):
        for kw in decision_keywords:
            if kw in combined_lower:
                match = re.search(rf"{kw}(.{{2,80}})", combined)
                if match:
                    return {"key": "决策记录", "value": match.group(1).strip()[:80]}

    # 技术栈/工具类
    tech_keywords = [
        "python", "javascript", "java", "go", "rust", "react",
        "vue", "django", "flask", "mysql", "redis", "docker",
        "git", "api", "数据库", "框架", "工具", "系统"
    ]
    if any(kw in combined_lower for kw in tech_keywords):
        # 查找技术栈相关描述
        match = re.search(r"(使用|采用|基于|框架是|用的是)(.{2,50})", combined)
        if match:
            return {"key": "技术栈", "value": match.group(2).strip()[:80]}

    # Phase 2: LLM 智能提取（如果规则未匹配，且对话较长）
    if len(combined) > 100:
        return _extract_with_llm(user_msg, assistant_msg)

    return None


def _extract_with_llm(user_msg: str, assistant_msg: str) -> dict:
    """LLM 智能提取重要信息"""
    try:
        from llm_client import llm_client
    except ImportError:
        return None

    prompt = f"""请从以下对话中提取一条最重要的记忆信息：

用户消息：{user_msg[:300]}
助手回复：{assistant_msg[:300]}

要求：
1. 只提取对用户有价值的信息（偏好、决策、任务结果、关键事实）
2. 输出格式：类型|内容（如：用户偏好|喜欢简洁的回答）
3. 如果没有重要信息，输出：无
4. 只输出一行"""

    try:
        success, result = llm_client.call(
            role="format",
            message=prompt,
            timeout=15
        )
        if success and result and result != "无":
            if "|" in result:
                parts = result.split("|", 1)
                return {"key": parts[0].strip(), "value": parts[1].strip()[:100]}
    except Exception as e:
        print(f"⚠️ LLM 提取失败: {e}")

    return None
