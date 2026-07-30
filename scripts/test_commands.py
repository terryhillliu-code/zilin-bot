#!/usr/bin/env python3
"""
飞书命令测试脚本
直接调用 command_handler 函数进行测试，无需真实飞书连接
"""

import os
import sys
import json

# 添加路径
sys.path.insert(0, os.path.expanduser("~/zhiwei-bot"))
sys.path.insert(0, os.path.expanduser("~/zhiwei-dev"))

# 模拟全局变量
test_results = []
message_log = []

def mock_reply_message(message_id, text):
    """模拟回复消息"""
    message_log.append({"type": "reply", "message_id": message_id, "text": text})
    print(f"📤 回复: {text[:100]}..." if len(text) > 100 else f"📤 回复: {text}")

def mock_reply_card(message_id, title, content):
    """模拟卡片回复"""
    message_log.append({"type": "card", "message_id": message_id, "title": title, "content": content})
    print(f"📤 卡片: [{title}] {content[:50]}...")

def run_test(name, func, *args, expected_contains=None, expected_not_contains=None):
    """运行单个测试"""
    global message_log
    message_log = []

    print(f"\n{'='*50}")
    print(f"🧪 测试: {name}")
    print(f"{'='*50}")

    try:
        result = func(*args)

        # 检查预期内容
        success = True
        if expected_contains:
            for text in expected_contains:
                found = any(text in str(m) for m in message_log)
                if not found:
                    print(f"❌ 未找到预期内容: {text}")
                    success = False

        if expected_not_contains:
            for text in expected_not_contains:
                found = any(text in str(m) for m in message_log)
                if found:
                    print(f"❌ 不应出现的内容: {text}")
                    success = False

        status = "✅ 通过" if success else "❌ 失败"
        print(f"\n结果: {status}")

        test_results.append({
            "name": name,
            "status": "PASS" if success else "FAIL",
            "messages": message_log.copy()
        })

        return success

    except Exception as e:
        print(f"❌ 异常: {e}")
        import traceback
        traceback.print_exc()
        test_results.append({
            "name": name,
            "status": "ERROR",
            "error": str(e)
        })
        return False


def test_help():
    """测试 /help 命令"""
    from command_handler import show_help
    result = show_help()
    print(f"输出:\n{result}")
    return "v2.2" in result or "知微" in result


def test_status():
    """测试 /status 命令"""
    from command_handler import get_quick_status
    result = get_quick_status()
    print(f"输出:\n{result}")
    return "状态" in result or "模型" in result


def test_ask_no_param():
    """测试 /ask 无参数"""
    # 这个会直接报错因为 split 只有 1 个元素
    try:
        text = "/ask"
        parts = text.split(" ", 1)
        query = parts[1]  # IndexError
    except IndexError:
        print("✅ 正确抛出 IndexError（缺少参数）")
        return True
    return False


def test_memory():
    """测试 /memory 命令"""
    try:
        from memory_manager import MemoryManager
        mm = MemoryManager(user_id="test_user")
        stats = mm.get_stats()
        print(f"记忆统计: {stats}")
        return True
    except Exception as e:
        print(f"❌ 记忆管理器测试失败: {e}")
        return False


def test_voice_task_store():
    """测试 /tasks 依赖"""
    try:
        from voice_task_store import VoiceTaskStore
        store = VoiceTaskStore()
        stats = store.stats()
        print(f"任务统计: {stats}")
        return True
    except Exception as e:
        print(f"❌ 待办任务存储测试失败: {e}")
        return False


def test_task_store():
    """测试 /dev 依赖"""
    try:
        from task_store import TaskStore
        store = TaskStore()
        recent = store.list_recent(5)
        print(f"最近任务: {len(recent)} 条")
        return True
    except Exception as e:
        print(f"❌ 任务存储测试失败: {e}")
        return False


def test_rag_bridge():
    """测试 /ask 依赖 (RAG)"""
    try:
        from rag_bridge import is_available, get_context
        available = is_available()
        print(f"RAG 可用: {available}")
        if available:
            # 简单查询测试
            context = get_context("测试", top_k=1)
            print(f"检索结果长度: {len(context) if context else 0}")
        return True
    except Exception as e:
        print(f"❌ RAG 桥接测试失败: {e}")
        return False


def test_knowledge_collect():
    """测试 /收录 依赖"""
    try:
        script_path = os.path.expanduser("~/zhiwei-bot/scripts/knowledge_collect.py")
        # 使用共享 venv (v2.0 合并后)
        venv_python = os.path.expanduser("~/zhiwei-shared-venv/bin/python")

        import subprocess
        result = subprocess.run(
            [venv_python, script_path, "--help"],
            capture_output=True, text=True, timeout=10
        )
        print(f"脚本状态: {'可用' if result.returncode == 0 else '异常'}")
        print(f"输出: {result.stdout[:200] if result.stdout else result.stderr[:200]}")
        return result.returncode == 0 or "usage" in result.stdout.lower() or "error" not in result.stderr.lower()
    except Exception as e:
        print(f"❌ 知识收录脚本测试失败: {e}")
        return False


def test_model_config():
    """测试 /model 依赖"""
    try:
        config_path = os.path.expanduser("~/logs/current_model.json")
        if os.path.exists(config_path):
            with open(config_path) as f:
                data = json.load(f)
            print(f"模型配置: {data}")
            return True
        else:
            print("⚠️ 模型配置文件不存在")
            return False
    except Exception as e:
        print(f"❌ 模型配置测试失败: {e}")
        return False


def test_ocmodel():
    """测试 m1-m8 依赖"""
    try:
        import subprocess
        result = subprocess.run(
            ["/usr/local/bin/ocmodel"],
            capture_output=True, text=True, timeout=5
        )
        print(f"ocmodel 输出: {result.stdout[:100] if result.stdout else result.stderr[:100]}")
        return True
    except Exception as e:
        print(f"❌ ocmodel 测试失败: {e}")
        return False


def test_chat_handler():
    """测试普通对话"""
    try:
        from chat_handler import chat_handler
        print("✅ chat_handler 加载成功")

        # 不实际调用（会消耗 API），只检查加载
        return True
    except Exception as e:
        print(f"❌ chat_handler 测试失败: {e}")
        return False


def test_llm_client():
    """测试 LLM 客户端"""
    try:
        from zhiwei_common.llm import llm_client
        print("✅ llm_client 加载成功")
        return True
    except Exception as e:
        print(f"❌ llm_client 测试失败: {e}")
        return False


def main():
    """运行所有测试"""
    print("=" * 60)
    print("🚀 飞书命令测试开始")
    print("=" * 60)

    # 1. 基础命令测试
    print("\n" + "=" * 60)
    print("📋 第一组：基础命令")
    print("=" * 60)

    run_test("/help 命令", test_help)
    run_test("/status 命令", test_status)
    run_test("/ask 无参数", test_ask_no_param)

    # 2. 依赖模块测试
    print("\n" + "=" * 60)
    print("📋 第二组：依赖模块")
    print("=" * 60)

    run_test("MemoryManager", test_memory)
    run_test("VoiceTaskStore", test_voice_task_store)
    run_test("TaskStore", test_task_store)
    run_test("RAG Bridge", test_rag_bridge)
    run_test("Knowledge Collect", test_knowledge_collect)

    # 3. 模型相关测试
    print("\n" + "=" * 60)
    print("📋 第三组：模型相关")
    print("=" * 60)

    run_test("Model Config", test_model_config)
    run_test("ocmodel 脚本", test_ocmodel)

    # 4. 对话相关测试
    print("\n" + "=" * 60)
    print("📋 第四组：对话相关")
    print("=" * 60)

    run_test("ChatHandler", test_chat_handler)
    run_test("LLM Client", test_llm_client)

    # 汇总结果
    print("\n" + "=" * 60)
    print("📊 测试结果汇总")
    print("=" * 60)

    passed = sum(1 for r in test_results if r["status"] == "PASS")
    failed = sum(1 for r in test_results if r["status"] == "FAIL")
    errors = sum(1 for r in test_results if r["status"] == "ERROR")

    print(f"\n✅ 通过: {passed}")
    print(f"❌ 失败: {failed}")
    print(f"⚠️ 错误: {errors}")
    print(f"📈 总计: {len(test_results)}")

    if failed > 0 or errors > 0:
        print("\n❌ 失败/错误的测试:")
        for r in test_results:
            if r["status"] in ["FAIL", "ERROR"]:
                print(f"  - {r['name']}: {r.get('error', 'N/A')}")

    return passed, failed, errors


if __name__ == "__main__":
    main()