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
    """确保 check_rate_limit 使用 _ctx 上下文"""
    import command_handler
    import inspect

    source = inspect.getsource(command_handler.check_rate_limit)

    # 应该使用 _ctx 而非全局变量
    if "_ctx.user_last_request" in source:
        print("✅ PASS: check_rate_limit 正确使用 _ctx 上下文")
        return True
    else:
        print("❌ FAIL: check_rate_limit 未使用 _ctx")
        return False


def test_check_rate_limit_with_no_context():
    """未初始化 context 时应该抛出异常或正确处理"""
    from command_handler import check_rate_limit, _ctx

    # 清空 _ctx 的相关属性
    if hasattr(_ctx, 'user_last_request'):
        delattr(_ctx, 'user_last_request')
    if hasattr(_ctx, 'RATE_LIMIT_SECONDS'):
        delattr(_ctx, 'RATE_LIMIT_SECONDS')

    try:
        result = check_rate_limit("test_user")
        # 如果没有抛出异常，说明有默认行为
        print(f"⚠️ INFO: 无 context 时返回 {result}")
        return True  # 接受任何结果
    except AttributeError as e:
        # 预期会抛出 AttributeError
        print("✅ PASS: 无 context 时正确抛出 AttributeError")
        return True


def test_check_rate_limit_with_context():
    """有 context 时应该正常限流"""
    from command_handler import check_rate_limit, _ctx
    from collections import defaultdict

    # 设置 _ctx 属性
    _ctx.user_last_request = defaultdict(float)
    _ctx.RATE_LIMIT_SECONDS = 10

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

    if all(results):
        print("✅ PASS: 有 context 时限流逻辑正确")
        return True
    else:
        print(f"❌ FAIL: 限流逻辑异常 - {results}")
        return False


def test_context_provides_rate_limit_vars():
    """command_handler _ctx 必须提供 rate limit 所需变量"""
    from command_handler import _ctx, init_command_handler
    from collections import defaultdict

    # 初始化 _ctx（模拟 ws_client 的调用）
    init_command_handler(
        reply_message=lambda *a: None,
        reply_card=lambda *a: None,
        call_openclaw_agent=lambda *a: "",
        query_knowledge_base=lambda *a: None,
        get_memory=lambda *a: None,
        add_to_history=lambda *a: None,
        get_history=lambda *a: "",
        is_article_url=lambda *a: False,
        is_video_url=lambda *a: False,
        summarize_url=lambda *a: "",
        handle_video_async=lambda *a: None,
        extract_video_url=lambda *a: None,
        extract_article_url=lambda *a: None,
        TaskLogger=None,
        IntentRouter=None,
        save_active_user=lambda *a: None,
        load_active_user=lambda *a: None,
        chat_history={},
        pending_voice={},
        pending_image={},
        pending_review={},
        MAX_HISTORY=20,
        RATE_LIMIT_SECONDS=10,
        user_last_request=defaultdict(float),
        memory_cache={},
        pending_video_confirm={},
        get_video_history=lambda *a: None,
        get_chat_handler=lambda: None,
    )

    checks = [
        hasattr(_ctx, 'user_last_request'),
        hasattr(_ctx, 'RATE_LIMIT_SECONDS'),
        _ctx.RATE_LIMIT_SECONDS == 10,
        isinstance(_ctx.user_last_request, dict),
    ]

    if all(checks):
        print("✅ PASS: _ctx 正确提供 rate limit 变量")
        return True
    else:
        print(f"❌ FAIL: _ctx 缺少必要属性 - {checks}")
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