#!/usr/bin/env python3
"""
通用测试文件 - 用于测试知微系统的基本功能组件
此文件用于测试各种通用功能和集成测试
"""

import unittest
import sys
import os
import tempfile
import shutil
from unittest.mock import patch, MagicMock

# 添加项目根目录到路径，以便导入模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

class TestGeneralFunctions(unittest.TestCase):
    """
    通用功能测试类 - 测试系统的基本功能组件
    """

    def setUp(self):
        """每个测试方法运行前的设置"""
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        """每个测试方法运行后的清理"""
        shutil.rmtree(self.temp_dir)

    def test_basic_assertion(self):
        """测试基本断言功能"""
        # 基本断言测试
        self.assertEqual(2 + 2, 4)
        self.assertTrue(True)
        self.assertFalse(False)

    def test_temp_directory_creation(self):
        """测试临时目录创建功能"""
        # 验证临时目录已创建
        self.assertTrue(os.path.exists(self.temp_dir))
        self.assertTrue(os.path.isdir(self.temp_dir))

        # 在临时目录中创建文件并验证
        test_file = os.path.join(self.temp_dir, 'test.txt')
        with open(test_file, 'w') as f:
            f.write('test content')

        self.assertTrue(os.path.exists(test_file))

        with open(test_file, 'r') as f:
            content = f.read()

        self.assertEqual(content, 'test content')

    def test_environment_variables(self):
        """测试环境变量访问"""
        # 设置一个临时环境变量用于测试
        test_var = 'TEST_VAR_FOR_ZHIWEI'
        test_value = 'test_value'
        os.environ[test_var] = test_value

        # 获取并验证
        retrieved_value = os.environ.get(test_var)
        self.assertEqual(retrieved_value, test_value)

        # 清理
        del os.environ[test_var]

    @patch('os.path.exists')
    def test_mock_example(self, mock_exists):
        """测试mock功能示例"""
        # 模拟文件存在
        mock_exists.return_value = True
        result = os.path.exists('/fake/path')
        self.assertTrue(result)

        # 模拟文件不存在
        mock_exists.return_value = False
        result = os.path.exists('/fake/path')
        self.assertFalse(result)


class TestSystemIntegration(unittest.TestCase):
    """
    系统集成测试类 - 测试多个组件的协同工作
    """

    def test_module_imports(self):
        """测试关键模块的导入"""
        # 测试一些关键模块是否可以成功导入
        try:
            import feishu_quota
            self.assertTrue(hasattr(feishu_quota, 'record_call'))
        except ImportError as e:
            # 如果缺少依赖，跳过这个测试
            self.skipTest(f"无法导入feishu_quota模块: {e}")

    def test_file_operations(self):
        """测试文件操作功能"""
        # 在临时目录中创建测试文件
        temp_dir = tempfile.mkdtemp()
        try:
            test_file = os.path.join(temp_dir, 'integration_test.txt')

            # 写入内容
            with open(test_file, 'w') as f:
                f.write('Integration test content\nLine 2\nLine 3')

            # 验证文件已创建
            self.assertTrue(os.path.exists(test_file))

            # 读取内容
            with open(test_file, 'r') as f:
                content = f.read()

            # 验证内容
            expected_content = 'Integration test content\nLine 2\nLine 3'
            self.assertEqual(content, expected_content)

        finally:
            # 清理
            shutil.rmtree(temp_dir)


class TestConfiguration(unittest.TestCase):
    """
    配置测试类 - 测试系统配置相关功能
    """

    def test_project_structure(self):
        """测试项目结构是否存在关键目录"""
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        # 检查关键目录是否存在
        key_dirs = [
            'zhiwei-bot',
            'zhiwei-scheduler',
            'Documents/Library',
            'clawdbot-docker',
            'scripts'
        ]

        for dir_name in key_dirs:
            dir_path = os.path.join(project_root, dir_name)
            self.assertTrue(
                os.path.exists(dir_path),
                f"关键目录不存在: {dir_path}"
            )

    def test_critical_files_exist(self):
        """测试关键文件是否存在"""
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        # 根据CLAUDE.md中提到的关键文件列表
        critical_files = [
            'zhiwei-bot/ws_client.py',
            'zhiwei-scheduler/task_executor.py',
            'scripts/pre_check_v2.sh',
            'clawdbot-docker/docker-compose.yml'
        ]

        for file_path in critical_files:
            full_path = os.path.join(project_root, file_path)
            self.assertTrue(
                os.path.exists(full_path),
                f"关键文件不存在: {full_path}"
            )


def run_integration_tests():
    """运行集成测试特定功能"""
    print("=== 运行集成测试 ===")

    # 检查Python版本
    print(f"Python版本: {sys.version}")

    # 检查项目路径
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    print(f"项目根目录: {project_root}")

    # 检查系统路径
    print(f"系统路径包含 {len(sys.path)} 个项目")


def run_suite():
    """运行所有测试的便捷函数"""
    # 创建测试套件
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    # 添加所有测试类
    suite.addTest(loader.loadTestsFromTestCase(TestGeneralFunctions))
    suite.addTest(loader.loadTestsFromTestCase(TestSystemIntegration))
    suite.addTest(loader.loadTestsFromTestCase(TestConfiguration))

    # 运行测试
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    return result


if __name__ == '__main__':
    print("知微系统通用测试")
    print("=" * 50)

    # 解析命令行参数
    if len(sys.argv) > 1:
        if sys.argv[1] == '--integration':
            # 只运行集成测试
            run_integration_tests()
        elif sys.argv[1] == '--suite':
            # 运行测试套件
            run_suite()
        else:
            # 显示帮助
            print("用法:")
            print("  python test_general.py              # 运行所有测试")
            print("  python test_general.py --integration # 只运行集成测试")
            print("  python test_general.py --suite       # 运行测试套件")
    else:
        # 默认行为：运行所有测试
        run_integration_tests()
        print("\n" + "=" * 50)
        unittest.main(argv=[''], verbosity=2, exit=False)