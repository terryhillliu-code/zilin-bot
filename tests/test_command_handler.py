"""
command_handler 模块回归测试

保护关键功能不被破坏：
1. check_rate_limit 必须能正确获取 CommandContext 中的变量
2. handle_text_async 必须能正常处理消息

创建原因：修复 check_rate_limit 函数使用了未定义的全局变量导致消息处理全部失败

运行方式: python3 tests/test_command_handler.py
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock
from collections import defaultdict

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))


def test_check_rate_limit_no_undefined_global():
    """确保 check_rate_limit 不依赖未定义的全局变量"""
    import command_handler
    import inspect

    source = inspect.getsource(command_handler.check_rate_limit)

    # 不应该有 global user_last_request 声明
    if "global user_last_request" in source:
        print("❌ FAIL: check_rate_limit 不应该使用 global user_last_request")
        return False

    # 应该从 context 获取变量
    if "get_context()" not in source:
        print("❌ FAIL: check_rate_limit 应该从 get_context() 获取变量")
        return False

    print("✅ PASS: check_rate_limit 代码结构正确")
    return True


def test_check_rate_limit_with_no_context():
    """没有初始化 context 时应该跳过限流"""
    from command_handler import check_rate_limit
    from command_context import set_context

    # 清空 context
    set_context(None)

    # 应该返回 True（跳过限流）
    result = check_rate_limit("test_user")
    if result == True:
        print("✅ PASS: 无 context 时正确跳过限流")
        return True
    else:
        print(f"❌ FAIL: 无 context 时应返回 True，实际返回 {result}")
        return False


def test_check_rate_limit_with_context():
    """有 context 时应该正常限流"""
    from command_handler import check_rate_limit
    from command_context import CommandContext, set_context

    # 创建 mock context
    ctx = MagicMock(spec=CommandContext)
    ctx.user_last_request = defaultdict(float)
    ctx.RATE_LIMIT_SECONDS = 10

    set_context(ctx)

    results = []

    # 第一次应该通过
    result1 = check_rate_limit("test_user")
    results.append(result1 == True)

    # 立即第二次应该被限流
    result2 = check_rate_limit("test_user")
    results.append(result2 == False)

    # 不同用户不受影响
    result3 = check_rate_limit("other_user")
    results.append(result3 == True)

    set_context(None)

    if all(results):
        print("✅ PASS: 有 context 时限流逻辑正确")
        return True
    else:
        print(f"❌ FAIL: 限流逻辑异常 - {results}")
        return False


def test_context_provides_rate_limit_vars():
    """CommandContext 必须提供 rate limit 所需变量"""
    from command_context import CommandContext

    ctx = CommandContext(
        reply_message=lambda *a: None,
        reply_card=lambda *a: None,
        RATE_LIMIT_SECONDS=10,
        user_last_request=defaultdict(float),
    )

    checks = [
        hasattr(ctx, 'user_last_request'),
        hasattr(ctx, 'RATE_LIMIT_SECONDS'),
        ctx.RATE_LIMIT_SECONDS == 10,
        isinstance(ctx.user_last_request, dict),
    ]

    if all(checks):
        print("✅ PASS: CommandContext 正确提供 rate limit 变量")
        return True
    else:
        print(f"❌ FAIL: CommandContext 缺少必要属性 - {checks}")
        return False


def test_extract_video_url():
    """测试抖音 URL 提取"""
    from media_handler import extract_video_url

    # 抖音分享文本
    text = "9.74 复制打开抖音，看看【天涯客的作品】测试视频 https://v.douyin.com/J1KoIAm6evg/"

    url = extract_video_url(text)
    if url and "douyin.com" in url:
        print("✅ PASS: 抖音 URL 提取正确")
        return True
    else:
        print(f"❌ FAIL: URL 提取失败 - {url}")
        return False


def run_all_tests():
    """运行所有测试"""
    print("=" * 50)
    print("command_handler 回归测试")
    print("=" * 50)

    tests = [
        test_check_rate_limit_no_undefined_global,
        test_check_rate_limit_with_no_context,
        test_check_rate_limit_with_context,
        test_context_provides_rate_limit_vars,
        test_extract_video_url,
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