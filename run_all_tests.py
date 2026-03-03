#!/usr/bin/env python3
"""
知微系统 - 综合测试执行器
批量运行所有测试文件并生成报告
"""

import unittest
import sys
import os
import time
from pathlib import Path

def run_all_tests():
    """运行所有测试并生成报告"""
    print("="*60)
    print("知微系统综合测试执行器")
    print("="*60)

    # 查找所有测试文件
    test_dir = Path(__file__).parent
    test_files = list(test_dir.glob("test_*.py"))

    print(f"发现 {len(test_files)} 个测试文件:")
    for tf in test_files:
        print(f"  - {tf.name}")

    print("\n开始执行测试...")
    print("-"*60)

    # 创建测试套件
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    # 添加所有测试文件
    for test_file in test_files:
        # 获取模块名（不含.py后缀）
        module_name = test_file.stem

        # 动态导入测试模块
        try:
            # 添加到Python路径
            sys.path.insert(0, str(test_dir))

            # 导入模块
            import importlib
            module = importlib.import_module(module_name)

            # 将模块的测试添加到套件
            suite.addTests(loader.loadTestsFromModule(module))

            print(f"✓ 加载测试模块: {module_name}")
        except ImportError as e:
            print(f"✗ 加载测试模块失败: {module_name} - {e}")
        finally:
            if str(test_dir) in sys.path:
                sys.path.remove(str(test_dir))

    # 运行测试
    start_time = time.time()

    runner = unittest.TextTestRunner(
        verbosity=2,
        stream=sys.stdout,
        buffer=True,  # 捕获测试中的输出
        descriptions=True
    )

    result = runner.run(suite)

    end_time = time.time()
    duration = end_time - start_time

    print("\n" + "="*60)
    print("测试执行完成")
    print("="*60)
    print(f"总运行时间: {duration:.2f}秒")
    print(f"运行测试数: {result.testsRun}")
    print(f"成功: {result.testsRun - len(result.failures) - len(result.errors)}")
    print(f"失败: {len(result.failures)}")
    print(f"错误: {len(result.errors)}")

    if result.failures:
        print(f"\n失败详情 ({len(result.failures)}):")
        for i, (test, traceback) in enumerate(result.failures, 1):
            print(f"\n{i}. {test}")
            print(f"   {traceback}")

    if result.errors:
        print(f"\n错误详情 ({len(result.errors)}):")
        for i, (test, traceback) in enumerate(result.errors, 1):
            print(f"\n{i}. {test}")
            print(f"   {traceback}")

    return len(result.failures) == 0 and len(result.errors) == 0

if __name__ == '__main__':
    success = run_all_tests()
    sys.exit(0 if success else 1)