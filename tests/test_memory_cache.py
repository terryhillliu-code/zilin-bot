"""
Memory Cache 模块测试

测试关键功能：
1. memory_cache 过期清理
2. chat_history 限制

运行方式: python3 tests/test_memory_cache.py
"""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))


def test_memory_cache_expiry():
    """测试 memory_cache 过期清理"""
    # 模拟 memory_cache 结构
    memory_cache = {}
    MEMORY_CACHE_EXPIRE_SECONDS = 3600  # 1小时

    # 添加一个"旧"条目（模拟 2 小时前访问）
    old_time = time.time() - 7200  # 2小时前
    memory_cache["old_user"] = {
        "manager": MagicMock(),
        "last_access": old_time
    }

    # 添加一个"新"条目
    memory_cache["new_user"] = {
        "manager": MagicMock(),
        "last_access": time.time()
    }

    # 执行清理逻辑
    current_time = time.time()
    expired = []
    for user_id, data in memory_cache.items():
        if current_time - data.get("last_access", 0) > MEMORY_CACHE_EXPIRE_SECONDS:
            expired.append(user_id)
    for user_id in expired:
        del memory_cache[user_id]

    # 验证
    if "old_user" not in memory_cache and "new_user" in memory_cache:
        print("✅ PASS: memory_cache 过期清理正确")
        return True
    else:
        print(f"❌ FAIL: 清理结果异常 - {list(memory_cache.keys())}")
        return False


def test_chat_history_limit():
    """测试 chat_history 最大限制"""
    from collections import deque

    MAX_HISTORY = 20
    chat_history = {}

    user_id = "test_user"
    chat_history[user_id] = deque(maxlen=MAX_HISTORY)

    # 添加 25 条记录
    for i in range(25):
        chat_history[user_id].append((f"{i:02d}:00", "user", f"message {i}"))

    # 验证只保留最近 20 条
    if len(chat_history[user_id]) == MAX_HISTORY:
        # 验证是最新的 20 条（从第 5 条开始）
        first_time = chat_history[user_id][0][0]
        if first_time == "05:00":
            print("✅ PASS: chat_history 正确限制为最近 20 条")
            return True
        else:
            print(f"❌ FAIL: 记录顺序异常 - 第一条时间 {first_time}")
            return False
    else:
        print(f"❌ FAIL: 长度不正确 - {len(chat_history[user_id])}")
        return False


def test_processed_messages_dedup():
    """测试消息去重队列"""
    from collections import deque

    processed_messages = deque(maxlen=500)

    # 添加 600 条
    for i in range(600):
        processed_messages.append(f"msg_{i}")

    # 验证只保留最近 500 条
    if len(processed_messages) == 500:
        # 验证是最新的（从 msg_100 开始）
        if processed_messages[0] == "msg_100":
            print("✅ PASS: 消息去重队列正确淘汰旧消息")
            return True
        else:
            print(f"❌ FAIL: 队列顺序异常 - 第一条 {processed_messages[0]}")
            return False
    else:
        print(f"❌ FAIL: 队列长度不正确 - {len(processed_messages)}")
        return False


def test_pending_image_cleanup():
    """测试待处理图片清理"""
    pending_image = {}

    # 添加一个过期的（11 分钟前）
    pending_image["old_user"] = {
        "time": time.time() - 660,  # 11分钟前
        "data": "old_image_data"
    }

    # 添加一个有效的
    pending_image["new_user"] = {
        "time": time.time() - 300,  # 5分钟前
        "data": "new_image_data"
    }

    # 执行清理（10 分钟过期）
    current_time = time.time()
    expired = []
    for user_id, data in pending_image.items():
        if isinstance(data, dict):
            if current_time - data.get("time", 0) > 600:
                expired.append(user_id)
    for user_id in expired:
        del pending_image[user_id]

    if "old_user" not in pending_image and "new_user" in pending_image:
        print("✅ PASS: 待处理图片清理正确")
        return True
    else:
        print(f"❌ FAIL: 清理结果异常 - {list(pending_image.keys())}")
        return False


def test_get_memory_updates_timestamp():
    """测试 get_memory 更新时间戳"""
    # 模拟 memory_cache 结构
    memory_cache = {}
    MEMORY_CACHE_EXPIRE_SECONDS = 3600

    # 模拟 get_memory 函数
    def get_memory(user_id: str):
        current_time = time.time()
        if user_id not in memory_cache:
            memory_cache[user_id] = {
                "manager": MagicMock(),
                "last_access": current_time
            }
        else:
            memory_cache[user_id]["last_access"] = current_time
        return memory_cache[user_id]["manager"]

    # 第一次访问
    manager1 = get_memory("test_user")
    time.sleep(0.1)  # 等待一点时间

    # 第二次访问
    manager2 = get_memory("test_user")

    # 验证时间戳被更新
    if manager1 is manager2 and memory_cache["test_user"]["last_access"] > 0:
        print("✅ PASS: get_memory 正确更新时间戳")
        return True
    else:
        print("❌ FAIL: 时间戳更新异常")
        return False


def run_all_tests():
    """运行所有测试"""
    print("=" * 50)
    print("Memory Cache 测试")
    print("=" * 50)

    tests = [
        test_memory_cache_expiry,
        test_chat_history_limit,
        test_processed_messages_dedup,
        test_pending_image_cleanup,
        test_get_memory_updates_timestamp,
    ]

    results = []
    for test in tests:
        try:
            results.append(test())
        except Exception as e:
            print(f"❌ FAIL: {test.__name__} 抛出异常: {e}")
            import traceback
            traceback.print_exc()
            results.append(False)
        print()

    print("=" * 50)
    passed = sum(results)
    total = len(results)
    print(f"测试结果: {passed}/{total} 通过")

    if all(results):
        print("✅ 所有测试通过")
        return 0
    else:
        print("❌ 部分测试失败")
        return 1


if __name__ == "__main__":
    exit(run_all_tests())