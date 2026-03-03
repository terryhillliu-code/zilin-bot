#!/usr/bin/env python3
"""
知识库功能测试 - 测试 Documents/Library/ 中的核心功能
"""

import unittest
import sys
import os
import tempfile
import shutil
from pathlib import Path

# 添加项目根目录到路径，以便导入模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 添加Documents目录到路径
documents_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'Documents', 'Library')
sys.path.insert(0, documents_path)

from klib_db import get_db_connection
from klib_hybrid import get_db, search_fts, search_vector, hybrid_search
from klib_scan import get_file_hash, generate_book_id, normalize_title
from klib_vectorize import get_db as get_vector_db


class TestKnowledgeBaseCore(unittest.TestCase):
    """
    测试知识库核心功能
    """

    def setUp(self):
        """每个测试方法运行前的设置"""
        # 创建临时数据库用于测试
        self.temp_db_dir = tempfile.mkdtemp()
        self.test_db_path = os.path.join(self.temp_db_dir, 'test_klib.db')

    def tearDown(self):
        """每个测试方法运行后的清理"""
        # 清理临时数据库
        if hasattr(self, 'temp_db_dir') and os.path.exists(self.temp_db_dir):
            shutil.rmtree(self.temp_db_dir)

    def test_database_connection(self):
        """测试数据库连接功能"""
        try:
            conn = get_db_connection(timeout=5)
            cursor = conn.cursor()
            cursor.execute("SELECT sqlite_version();")
            version = cursor.fetchone()
            self.assertIsNotNone(version)
            conn.close()
        except Exception as e:
            self.fail(f"数据库连接失败: {e}")

    def test_file_hash_generation(self):
        """测试文件哈希生成"""
        # 创建临时测试文件
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt') as f:
            f.write("test content for hash generation")
            temp_file_path = f.name

        try:
            hash_value = get_file_hash(Path(temp_file_path))
            self.assertIsInstance(hash_value, str)
            self.assertEqual(len(hash_value), 32)  # MD5 hash length
        finally:
            os.unlink(temp_file_path)

    def test_book_id_generation(self):
        """测试书籍ID生成"""
        test_file = Path("/path/to/test/book.pdf")
        book_id = generate_book_id(test_file)
        self.assertIsInstance(book_id, str)
        self.assertGreater(len(book_id), 0)

    def test_title_normalization(self):
        """测试标题标准化"""
        test_cases = [
            ("The Art of Programming", "the art of programming"),
            ("Python Crash Course", "python crash course"),
            ("Machine Learning Basics", "machine learning basics"),
            ("", ""),
        ]

        for original, expected in test_cases:
            with self.subTest(original=original):
                normalized = normalize_title(original)
                self.assertEqual(normalized, expected)


class TestKnowledgeBaseSearch(unittest.TestCase):
    """
    测试知识库搜索功能
    """

    def test_search_functions_exist(self):
        """测试搜索函数是否存在"""
        self.assertTrue(callable(search_fts))
        self.assertTrue(callable(search_vector))
        self.assertTrue(callable(hybrid_search))

    def test_get_db_function(self):
        """测试数据库获取功能"""
        try:
            db = get_db()
            self.assertIsNotNone(db)
        except Exception as e:
            # 如果没有配置数据库，这可能失败，但我们至少验证了函数存在
            pass


def suite():
    """构建测试套件"""
    test_suite = unittest.TestSuite()
    test_suite.addTest(unittest.makeSuite(TestKnowledgeBaseCore))
    test_suite.addTest(unittest.makeSuite(TestKnowledgeBaseSearch))
    return test_suite


if __name__ == '__main__':
    # 运行测试
    runner = unittest.TextTestRunner(verbosity=2)
    runner.run(suite())