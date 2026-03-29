"""
记忆系统集成模块

提供长期记忆能力：
- 用户偏好记忆
- 对话上下文记忆
- 自动记忆提取和检索
"""
import os
import json
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime

logger = logging.getLogger(__name__)

# 记忆数据目录
MEMORY_DATA_DIR = Path(__file__).parent.parent / "mem0_data"
MEMORY_DATA_DIR.mkdir(parents=True, exist_ok=True)

# 记忆存储文件
MEMORY_STORE_FILE = MEMORY_DATA_DIR / "memories.json"


class MemoryStore:
    """记忆存储"""

    def __init__(self):
        self.memories: Dict[str, List[Dict]] = {}
        self._load()

    def _load(self):
        """加载记忆"""
        if MEMORY_STORE_FILE.exists():
            try:
                with open(MEMORY_STORE_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        self.memories = data
                    else:
                        self.memories = {}
                logger.info(f"✅ 加载了 {sum(len(v) for v in self.memories.values())} 条记忆")
            except Exception as e:
                logger.warning(f"加载记忆失败: {e}")
                self.memories = {}

    def _save(self):
        """保存记忆"""
        try:
            with open(MEMORY_STORE_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.memories, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存记忆失败: {e}")

    def add(self, content: str, user_id: str, metadata: Optional[Dict] = None):
        """添加记忆"""
        if not isinstance(content, str):
            content = str(content)

        if user_id not in self.memories:
            self.memories[user_id] = []

        entry = {
            "id": f"mem_{datetime.now().strftime('%Y%m%d%H%M%S%f')}",
            "content": content,
            "metadata": metadata or {},
            "created_at": datetime.now().isoformat()
        }
        self.memories[user_id].append(entry)
        self._save()
        logger.debug(f"添加记忆: {content[:50]}...")

    def add_from_conversation(self, user_msg: str, assistant_msg: str, user_id: str):
        """从对话中提取并添加记忆"""
        try:
            from zhiwei_common.llm import llm_client

            prompt = f"""从对话中提取用户的重要信息（姓名、年龄、职业、偏好等），每行一条：

用户: {user_msg}
助手: {assistant_msg}

只输出提取的信息，不要其他内容。如果无重要信息，输出"无"。"""

            success, result = llm_client.call("chat", prompt, timeout=30)

            if success and result:
                result = result.strip()
                if result != "无":
                    for line in result.split('\n'):
                        fact = line.strip().lstrip('- ')
                        if len(fact) > 3:
                            self.add(fact, user_id, {"source": "conversation"})
                    return
        except Exception as e:
            logger.warning(f"LLM 提取失败: {e}")

        if len(user_msg) > 5:
            self.add(f"用户说: {user_msg[:80]}", user_id, {"source": "fallback"})

    def search(self, query: str, user_id: str, limit: int = 5) -> List[Dict]:
        """搜索记忆"""
        if user_id not in self.memories:
            return []

        results = []
        query_lower = query.lower()

        for mem in self.memories[user_id]:
            content = mem.get("content", "")
            if isinstance(content, list):
                content = " ".join(str(c) for c in content)
            elif not isinstance(content, str):
                content = str(content)

            if any(word in content.lower() for word in query_lower.split() if len(word) > 1):
                results.append({"memory": content, "score": 1})

        return results[:limit]

    def get_all(self, user_id: str) -> List[Dict]:
        """获取所有记忆"""
        return self.memories.get(user_id, [])

    def clear(self, user_id: str):
        """清除用户记忆"""
        if user_id in self.memories:
            del self.memories[user_id]
            self._save()


# 全局实例
_store = None

def _get_store() -> MemoryStore:
    global _store
    if _store is None:
        _store = MemoryStore()
    return _store


# 公开 API
def add_memory(content: str, user_id: str, metadata: Optional[Dict] = None) -> bool:
    """添加记忆"""
    _get_store().add(content, user_id, metadata)
    return True


def add_conversation_memory(user_msg: str, assistant_msg: str, user_id: str) -> bool:
    """从对话添加记忆"""
    _get_store().add_from_conversation(user_msg, assistant_msg, user_id)
    return True


def search_memory(query: str, user_id: str, limit: int = 5) -> List[Dict]:
    """搜索记忆"""
    return _get_store().search(query, user_id, limit)


def get_all_memories(user_id: str) -> List[Dict]:
    """获取所有记忆"""
    return _get_store().get_all(user_id)


def build_memory_context(user_id: str, query: str, limit: int = 5) -> str:
    """构建记忆上下文"""
    memories = search_memory(query, user_id, limit)
    if not memories:
        return ""
    lines = ["[用户记忆]"]
    for m in memories:
        lines.append(f"- {m.get('memory', '')}")
    return "\n".join(lines)


def reset_user_memories(user_id: str) -> bool:
    """重置用户记忆"""
    _get_store().clear(user_id)
    return True


# 兼容接口
get_mem0 = _get_store