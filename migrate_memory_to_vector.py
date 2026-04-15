#!/usr/bin/env python3
"""
迁移现有记忆数据到向量存储格式

用法：
    ~/zhiwei-shared-venv/bin/python migrate_memory_to_vector.py
"""
import os
import sys
import json
from pathlib import Path

# 添加 zhiwei-bot 路径
sys.path.insert(0, str(Path(__file__).parent))

from memory_manager import MemoryManager, MemoryVectorStore, MemoryVector, HAS_LANCEDB


def migrate_user_memory(user_id: str, memory_dir: str):
    """迁移单个用户的记忆数据"""
    print(f"\n迁移用户: {user_id}")

    # 读取现有状态文件
    state_file = os.path.join(memory_dir, f"{user_id}_state.json")
    persistent_file = os.path.join(memory_dir, f"{user_id}_persistent.json")

    # 检查文件是否存在
    if not os.path.exists(state_file):
        print(f"  ⚠️ 状态文件不存在: {state_file}")
        return 0

    # 初始化向量存储
    if not HAS_LANCEDB:
        print("  ❌ LanceDB 未安装，无法迁移")
        return 0

    vector_store = MemoryVectorStore()

    # 读取工作记忆
    count = 0
    try:
        with open(state_file, 'r') as f:
            state = json.load(f)

        working_memory = state.get("working_memory", [])
        summary = state.get("summary", "")

        # 存储工作记忆轮次
        for i, turn in enumerate(working_memory):
            user_msg = turn.get("user", "")
            assistant_msg = turn.get("assistant", "")
            timestamp = turn.get("time", "")

            # 创建记忆向量
            memory = MemoryVector(
                id=f"{user_id}_turn_{i}",
                user_id=user_id,
                text=f"用户: {user_msg}\n助手: {assistant_msg}",
                user_msg=user_msg,
                assistant_msg=assistant_msg,
                memory_type="conversation",
                timestamp=timestamp,
            )
            vector_store.add_memory(memory)
            count += 1

        # 存储摘要
        if summary:
            memory = MemoryVector(
                id=f"{user_id}_summary",
                user_id=user_id,
                text=f"历史摘要: {summary}",
                user_msg="",
                assistant_msg=summary,
                memory_type="summary",
                timestamp=state.get("updated", ""),
            )
            vector_store.add_memory(memory)
            count += 1

        print(f"  ✅ 工作记忆迁移: {count} 条")

    except Exception as e:
        print(f"  ❌ 工作记忆迁移失败: {e}")

    # 读取持久记忆
    if os.path.exists(persistent_file):
        try:
            with open(persistent_file, 'r') as f:
                persistent = json.load(f)

            for key, value in persistent.items():
                memory = MemoryVector(
                    id=f"{user_id}_persistent_{key}",
                    user_id=user_id,
                    text=f"{key}: {value.get('value', '')}",
                    user_msg="",
                    assistant_msg=value.get('value', ''),
                    memory_type="preference",
                    timestamp=value.get('time', ''),
                )
                vector_store.add_memory(memory)
                count += 1

            print(f"  ✅ 持久记忆迁移: {len(persistent)} 条")

        except Exception as e:
            print(f"  ❌ 持久记忆迁移失败: {e}")

    return count


def main():
    """主函数"""
    memory_dir = os.path.expanduser("~/logs/memory")

    # 查找所有用户
    users = set()
    for filename in os.listdir(memory_dir):
        if filename.endswith("_state.json"):
            # 提取 user_id
            user_id = filename.replace("_state.json", "")
            users.add(user_id)

    if not users:
        print("❌ 未找到现有记忆数据")
        return

    print(f"找到 {len(users)} 个用户的记忆数据")

    total_count = 0
    for user_id in users:
        count = migrate_user_memory(user_id, memory_dir)
        total_count += count

    print(f"\n✅ 迁移完成: 共 {total_count} 条记忆向量")


if __name__ == "__main__":
    main()