#!/usr/bin/env python3
"""
知微系统 - 测试工具集
用于辅助各种测试场景
"""

import os
import sys
import tempfile
import shutil
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

def create_temp_db_for_testing():
    """创建临时数据库用于测试"""
    temp_dir = tempfile.mkdtemp()
    temp_db_path = os.path.join(temp_dir, 'test_klib.db')

    # 创建测试数据库结构
    import sqlite3
    conn = sqlite3.connect(temp_db_path)
    cursor = conn.cursor()

    # 创建books表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS books (
            book_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            author TEXT,
            file_path TEXT UNIQUE,
            file_type TEXT,
            file_size INTEGER,
            file_hash TEXT,
            category TEXT DEFAULT '待整理',
            priority TEXT DEFAULT 'reference',
            language TEXT DEFAULT 'zh',
            created_at REAL,
            updated_at REAL
        )
    ''')

    conn.commit()
    conn.close()

    return temp_dir, temp_db_path

def cleanup_temp_db(temp_dir):
    """清理临时数据库"""
    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir)

def get_test_data_path():
    """获取测试数据路径"""
    test_data_dir = os.path.join(os.path.dirname(__file__), 'test_data')
    os.makedirs(test_data_dir, exist_ok=True)
    return test_data_dir

def create_mock_file(content, extension='.txt'):
    """创建带特定内容的模拟文件"""
    fd, path = tempfile.mkstemp(suffix=extension)
    try:
        with os.fdopen(fd, 'w') as tmp:
            tmp.write(content)
    except:
        os.close(fd)
        raise
    return path

def run_all_tests():
    """运行所有测试"""
    import unittest

    # 发现并运行所有测试
    loader = unittest.TestLoader()
    start_dir = os.path.dirname(__file__)
    suite = loader.discover(start_dir, pattern='test_*.py')

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    return result.wasSuccessful()

if __name__ == '__main__':
    print("知微系统测试工具集")
    print("功能列表:")
    print("1. 创建临时数据库用于测试")
    print("2. 运行所有测试")
    print("3. 创建模拟文件")

    if len(sys.argv) > 1 and sys.argv[1] == '--run-all':
        success = run_all_tests()
        sys.exit(0 if success else 1)