"""
LLM Client 模块测试

测试关键功能：
1. 熔断机制触发和恢复
2. 降级链调用
3. 统计记录

运行方式: python3 tests/test_llm_client.py
"""

import sys
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "zhiwei-common"))


def test_circuit_breaker_triggers_at_3_fails():
    """连续失败 3 次后触发熔断"""
    from zhiwei_common.llm import LLMClient, LLMConfig

    # 使用临时统计文件
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        stats_file = Path(f.name)
        f.write(json.dumps({
            "coding_plan": {"success": 0, "fail": 3, "consecutive_fail": 3,
                            "last_success": None, "last_fail": "2026-04-05T00:00:00"},
            "dashscope": {"success": 0, "fail": 0, "consecutive_fail": 0},
            "openrouter": {"success": 0, "fail": 0, "consecutive_fail": 0}
        }))

    try:
        client = LLMClient(LLMConfig())
        client._stats_file = stats_file
        client._stats = client._load_stats()

        # 调用应该被熔断
        success, msg = client.call("chat", "test message")

        if not success and "熔断" in msg:
            print("✅ PASS: 熔断正确触发")
            return True
        else:
            print(f"❌ FAIL: 熔断未触发 - success={success}, msg={msg}")
            return False
    finally:
        stats_file.unlink(missing_ok=True)


def test_circuit_breaker_reset():
    """熔断器重置功能"""
    from zhiwei_common.llm import LLMClient

    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        stats_file = Path(f.name)
        f.write(json.dumps({
            "coding_plan": {"success": 0, "fail": 5, "consecutive_fail": 5},
            "dashscope": {"success": 0, "fail": 0, "consecutive_fail": 0},
            "openrouter": {"success": 0, "fail": 0, "consecutive_fail": 0}
        }))

    try:
        client = LLMClient()
        client._stats_file = stats_file
        client._stats = client._load_stats()

        # 重置熔断器
        result = client.reset_circuit_breaker("coding_plan")

        if result and client._stats["coding_plan"]["consecutive_fail"] == 0:
            print("✅ PASS: 熔断器重置成功")
            return True
        else:
            print(f"❌ FAIL: 熔断器重置失败 - result={result}")
            return False
    finally:
        stats_file.unlink(missing_ok=True)


def test_reset_all_circuit_breakers():
    """重置所有熔断器"""
    from zhiwei_common.llm import LLMClient

    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        stats_file = Path(f.name)
        f.write(json.dumps({
            "coding_plan": {"consecutive_fail": 5},
            "dashscope": {"consecutive_fail": 3},
            "openrouter": {"consecutive_fail": 2}
        }))

    try:
        client = LLMClient()
        client._stats_file = stats_file
        client._stats = client._load_stats()

        client.reset_all_circuit_breakers()

        all_zero = all(
            client._stats[api]["consecutive_fail"] == 0
            for api in client._stats
        )

        if all_zero:
            print("✅ PASS: 所有熔断器重置成功")
            return True
        else:
            print("❌ FAIL: 部分熔断器未重置")
            return False
    finally:
        stats_file.unlink(missing_ok=True)


def test_get_circuit_breaker_status():
    """获取熔断器状态"""
    from zhiwei_common.llm import LLMClient

    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        stats_file = Path(f.name)
        f.write(json.dumps({
            "coding_plan": {"consecutive_fail": 3, "last_fail": "2026-04-05T00:00:00"},
            "dashscope": {"consecutive_fail": 0, "last_fail": None},
            "openrouter": {"consecutive_fail": 0, "last_fail": None}
        }))

    try:
        client = LLMClient()
        client._stats_file = stats_file
        client._stats = client._load_stats()

        status = client.get_circuit_breaker_status()

        checks = [
            "coding_plan" in status,
            status["coding_plan"]["tripped"] == True,
            status["coding_plan"]["consecutive_fail"] == 3,
            status["dashscope"]["tripped"] == False,
        ]

        if all(checks):
            print("✅ PASS: 熔断器状态获取正确")
            return True
        else:
            print(f"❌ FAIL: 状态不正确 - {status}")
            return False
    finally:
        stats_file.unlink(missing_ok=True)


def test_record_success_resets_consecutive_fail():
    """成功调用重置连续失败计数"""
    from zhiwei_common.llm import LLMClient

    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        stats_file = Path(f.name)
        f.write(json.dumps({
            "coding_plan": {"success": 0, "fail": 5, "consecutive_fail": 5},
            "dashscope": {"success": 0, "fail": 0, "consecutive_fail": 0},
            "openrouter": {"success": 0, "fail": 0, "consecutive_fail": 0}
        }))

    try:
        client = LLMClient()
        client._stats_file = stats_file
        client._stats = client._load_stats()

        client._record_success("coding_plan")

        if client._stats["coding_plan"]["consecutive_fail"] == 0:
            print("✅ PASS: 成功调用重置连续失败计数")
            return True
        else:
            print("❌ FAIL: 连续失败计数未重置")
            return False
    finally:
        stats_file.unlink(missing_ok=True)


def run_all_tests():
    """运行所有测试"""
    print("=" * 50)
    print("LLM Client 测试")
    print("=" * 50)

    tests = [
        test_circuit_breaker_triggers_at_3_fails,
        test_circuit_breaker_reset,
        test_reset_all_circuit_breakers,
        test_get_circuit_breaker_status,
        test_record_success_resets_consecutive_fail,
    ]

    results = []
    for test in tests:
        try:
            results.append(test())
        except Exception as e:
            print(f"❌ FAIL: {test.__name__} 抛出异常: {e}")
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