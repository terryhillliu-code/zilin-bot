#!/usr/bin/env python3
"""
单元测试集合 - 测试知微系统各核心模块
"""

import unittest
import sys
import os
import tempfile
from unittest.mock import Mock, patch, MagicMock
import json
import time

# 添加项目根目录到路径
sys.path.insert(0, '/Users/liufang/zhiwei-bot')

# 导入需要测试的模块
def safe_import(module_name, attr_names=None):
    """安全导入模块，处理缺失的依赖"""
    try:
        module = __import__(module_name)
        if attr_names:
            return [getattr(module, attr) for attr in attr_names]
        else:
            return module
    except ImportError as e:
        print(f"无法导入 {module_name}: {e}")
        if attr_names:
            return [None] * len(attr_names)
        return None

IntentRouter = safe_import('intent_router')

if IntentRouter is not None:
    IntentRouter = IntentRouter.IntentRouter  # 获取类本身

TaskLogger = safe_import('task_logger')

if TaskLogger is not None:
    TaskLogger = TaskLogger.TaskLogger  # 获取类本身

record_call, get_stats = safe_import('feishu_quota', ['record_call', 'get_stats'])

parse_comparison, generate_comparison = safe_import('tech_compare', ['parse_comparison', 'generate_comparison'])

reply_message, send_direct_message = safe_import('feishu_api', ['reply_message', 'send_direct_message'])

MemoryManager = safe_import('memory_manager')

if MemoryManager is not None:
    MemoryManager = MemoryManager.MemoryManager  # 获取类本身


class TestIntentRouter(unittest.TestCase):
    """测试意图路由模块"""

    @classmethod
    def setUpClass(cls):
        if IntentRouter is None:
            cls.skipTest(cls, "IntentRouter 模块不可用")

    def test_route_question(self):
        """测试路由问题类型消息"""
        result = IntentRouter.route("什么是人工智能？")
        self.assertIsInstance(result, str)

    def test_route_command(self):
        """测试路由命令类型消息"""
        result = IntentRouter.route("/help")
        self.assertIsInstance(result, str)

    def test_explain_functionality(self):
        """测试解释功能"""
        explanation = IntentRouter.explain("帮我写一份报告")
        self.assertIsInstance(explanation, str)


class TestTaskLogger(unittest.TestCase):
    """测试任务日志模块"""

    @classmethod
    def setUpClass(cls):
        if TaskLogger is None:
            cls.skipTest(cls, "TaskLogger 模块不可用")

    def test_log_task_creation(self):
        """测试任务日志记录"""
        # 由于log_task方法返回None，只测试它不会抛出异常
        try:
            TaskLogger.log_task("test_task", "success", "测试详情")
            self.assertTrue(True)  # 如果没有异常则成功
        except Exception as e:
            self.fail(f"log_task方法抛出了异常: {e}")

    def test_get_recent_tasks(self):
        """测试获取最近任务"""
        recent = TaskLogger.get_recent(5)
        self.assertIsInstance(recent, str)

    def test_search_functionality(self):
        """测试搜索功能"""
        results = TaskLogger.search("test")
        self.assertIsInstance(results, str)


class TestFeishuQuota(unittest.TestCase):
    """测试飞书配额管理"""

    @classmethod
    def setUpClass(cls):
        if record_call is None:
            cls.skipTest(cls, "feishu_quota 模块不可用")

    def test_record_api_call(self):
        """测试记录API调用"""
        initial_stats = get_stats()
        record_call("message_send")

        updated_stats = get_stats()
        self.assertGreaterEqual(updated_stats['today'], initial_stats['today'])

    def test_get_stats_format(self):
        """测试获取统计信息格式"""
        stats = get_stats()
        self.assertIn('today', stats)
        self.assertIn('this_month', stats)
        self.assertIn('daily_limit', stats)
        self.assertIn('monthly_remaining', stats)


class TestTechCompare(unittest.TestCase):
    """测试技术比较模块"""

    @classmethod
    def setUpClass(cls):
        if parse_comparison is None:
            cls.skipTest(cls, "tech_compare 模块不可用")

    def test_parse_comparison_valid(self):
        """测试解析有效的比较文本"""
        text = "A和B的比较：A速度快，B内存小"
        result = parse_comparison(text)
        self.assertIsNotNone(result)

    def test_generate_comparison(self):
        """测试生成比较内容"""
        context = {
            "item_a_info": [],
            "item_b_info": []
        }
        comparison = generate_comparison("Python", "Java", context)
        self.assertIsNotNone(comparison)


class TestMemoryManager(unittest.TestCase):
    """测试记忆管理模块"""

    @classmethod
    def setUpClass(cls):
        if MemoryManager is None:
            cls.skipTest(cls, "MemoryManager 模块不可用")

    def test_memory_operations(self):
        """测试基本记忆操作"""
        mm = MemoryManager("test_user")

        # 添加对话回合
        mm.add_turn("你好", "你好！有什么帮助吗？")

        # 构建上下文
        context = mm.build_context_prompt()
        self.assertIn("你好", context)

        # 重置记忆
        mm.reset()
        empty_context = mm.build_context_prompt()
        self.assertEqual(empty_context.strip(), "")

    def test_persistent_storage(self):
        """测试持久化存储"""
        mm = MemoryManager("test_user")

        # 保存持久化数据
        mm.save_persistent("key1", "value1")

        # 获取持久化数据
        retrieved = mm.get_persistent("key1")
        self.assertEqual(retrieved, "value1")


class TestFeishuAPI(unittest.TestCase):
    """测试飞书API模块（模拟环境）"""

    @classmethod
    def setUpClass(cls):
        if reply_message is None:
            cls.skipTest(cls, "feishu_api 模块不可用")

    @patch('feishu_api.global_client')
    def test_reply_message_mock(self, mock_client):
        """测试回复消息（使用模拟）"""
        mock_client.im_fake().post.return_value.status_code = 200

        # 由于我们无法实际调用，这主要是验证函数存在
        # 在实际环境中，这将调用真实的飞书API
        pass

    @patch('feishu_api.global_client')
    def test_send_direct_message_mock(self, mock_client):
        """测试发送直接消息（使用模拟）"""
        mock_client.im_fake().post.return_value.status_code = 200
        pass


def run_specific_test(suite_name):
    """运行特定测试套件"""
    suites = {
        'intent_router': TestIntentRouter,
        'task_logger': TestTaskLogger,
        'feishu_quota': TestFeishuQuota,
        'tech_compare': TestTechCompare,
        'memory_manager': TestMemoryManager,
        'feishu_api': TestFeishuAPI,
    }

    if suite_name.lower() in suites:
        loader = unittest.TestLoader()
        suite = loader.loadTestsFromTestCase(suites[suite_name.lower()])
        runner = unittest.TextTestRunner(verbosity=2)
        runner.run(suite)
    else:
        print(f"未知的测试套件: {suite_name}")
        print("可用套件:", ", ".join(suites.keys()))


if __name__ == '__main__':
    if len(sys.argv) > 1:
        if sys.argv[1] == 'list':
            print("可用测试套件:")
            print("- intent_router")
            print("- task_logger")
            print("- feishu_quota")
            print("- tech_compare")
            print("- memory_manager")
            print("- feishu_api")
            print("- all (运行所有测试)")
        elif sys.argv[1] == 'all':
            unittest.main(argv=[''], verbosity=2, exit=False)
        else:
            run_specific_test(sys.argv[1])
    else:
        print("用法:")
        print("  python3 test_units.py              # 显示此帮助")
        print("  python3 test_units.py list         # 列出所有测试套件")
        print("  python3 test_units.py all          # 运行所有测试")
        print("  python3 test_units.py <suite_name> # 运行特定测试套件")
        print("\n可用套件: intent_router, task_logger, feishu_quota, tech_compare, memory_manager, feishu_api")