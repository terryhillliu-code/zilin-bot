"""记忆管理器缓存与过期清理（2026-07-30 从 ws_client.py 拆分）

memory_cache 为共享可变 dict，ws_client 导入后原样注入 command_handler，
行为与拆分前完全一致。
"""

import time

from memory_manager import MemoryManager

# 记忆管理器缓存（user_id -> {manager, last_access_time}）
# v69.0: 添加时间戳支持过期清理
memory_cache = {}
MEMORY_CACHE_EXPIRE_SECONDS = 3600  # 1小时未访问则清理


def get_memory(user_id: str) -> MemoryManager:
    """获取或创建用户的记忆管理器（v69.0: 添加时间戳）"""
    current_time = time.time()
    if user_id not in memory_cache:
        memory_cache[user_id] = {
            "manager": MemoryManager(user_id),
            "last_access": current_time
        }
    else:
        memory_cache[user_id]["last_access"] = current_time
    return memory_cache[user_id]["manager"]


def cleanup_memory_cache():
    """清理过期的记忆管理器缓存（v69.0 新增）"""
    current_time = time.time()
    expired = []
    for user_id, data in memory_cache.items():
        if current_time - data.get("last_access", 0) > MEMORY_CACHE_EXPIRE_SECONDS:
            expired.append(user_id)
    for user_id in expired:
        del memory_cache[user_id]
    if expired:
        print(f"[Cleanup] 清理 {len(expired)} 个过期记忆缓存")
