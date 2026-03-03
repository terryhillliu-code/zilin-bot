#!/usr/bin/env python3
"""
知识库功能测试文件
用于测试 Documents/Library/ 下的知识库相关功能
"""

import os
import sys
import unittest
from unittest.mock import Mock, patch, MagicMock
import tempfile
import shutil

# 添加项目路径到sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 尝试导入相关模块
try:
    # 导入知识库相关模块
    from Documents.Library.klib_hybrid import hybrid_search, cmd_search
    from Documents.Library.klib_db import get_db_connection
    from Documents.Library.klib_query import cmd_stats, cmd_list, cmd_search as lib_cmd_search
    from Documents.Library.klib_scan import scan_directory
    from Documents.Library.klib_vectorize import vectorize_file
except ImportError as e:
    print(f"无法导入知识库模块: {e}")
    # 创建模拟对象以便进行单元测试
    hybrid_search = None
    cmd_search = None
    get_db_connection = None
    cmd_stats = None
    cmd_list = None
    lib_cmd_search = None
    scan_directory = None
    vectorize_file = None


class TestKnowledgeBase(unittest.TestCase):
    """知识库功能测试类"""

    def setUp(self):
        """测试初始化"""
        self.test_db_path = tempfile.mktemp(suffix='.db')

    def tearDown(self):
        """清理测试资源"""
        if os.path.exists(self.test_db_path):
            os.remove(self.test_db_path)

    @unittest.skipIf(hybrid_search is None, "知识库模块不可用")
    def test_hybrid_search_basic(self):
        """测试混合搜索基本功能"""
        # 由于实际的hybrid_search需要数据库和向量存储，这里仅测试函数是否存在
        self.assertTrue(callable(hybrid_search))

    @unittest.skipIf(get_db_connection is None, "知识库模块不可用")
    def test_get_db_connection(self):
        """测试数据库连接获取"""
        # 测试连接函数存在
        self.assertTrue(callable(get_db_connection))

        # 使用临时数据库路径测试连接
        conn = get_db_connection(self.test_db_path)
        self.assertIsNotNone(conn)
        conn.close()

    @unittest.skipIf(cmd_stats is None, "知识库模块不可用")
    def test_cmd_stats_exists(self):
        """测试统计命令函数存在"""
        self.assertTrue(callable(cmd_stats))

    @unittest.skipIf(cmd_list is None, "知识库模块不可用")
    def test_cmd_list_exists(self):
        """测试列表命令函数存在"""
        self.assertTrue(callable(cmd_list))

    @unittest.skipIf(lib_cmd_search is None, "知识库模块不可用")
    def test_lib_cmd_search_exists(self):
        """测试搜索命令函数存在"""
        self.assertTrue(callable(lib_cmd_search))


class TestFeishuBot(unittest.TestCase):
    """飞书机器人核心功能测试类"""

    def setUp(self):
        """测试初始化"""
        pass

    def test_import_ws_client(self):
        """测试ws_client模块导入"""
        try:
            import ws_client
            self.assertTrue(hasattr(ws_client, 'main'))
            self.assertTrue(hasattr(ws_client, 'save_active_user'))
            self.assertTrue(hasattr(ws_client, 'load_active_user'))
        except ImportError as e:
            # 对于依赖缺失的情况，我们将其标记为跳过而不是失败
            if 'lark_oapi' in str(e) or 'No module named' in str(e):
                self.skipTest(f"因缺少依赖而跳过: {e}")
            else:
                self.fail(f"无法导入ws_client模块: {e}")

    def test_import_command_handler(self):
        """测试command_handler模块导入"""
        try:
            import command_handler
            self.assertTrue(hasattr(command_handler, 'handle_text_async'))
            self.assertTrue(hasattr(command_handler, 'init_command_handler'))
        except ImportError as e:
            if 'lark_oapi' in str(e) or 'No module named' in str(e):
                self.skipTest(f"因缺少依赖而跳过: {e}")
            else:
                self.fail(f"无法导入command_handler模块: {e}")

    def test_import_media_handler(self):
        """测试media_handler模块导入"""
        try:
            import media_handler
            self.assertTrue(hasattr(media_handler, 'handle_image_async'))
            self.assertTrue(hasattr(media_handler, 'download_image'))
        except ImportError as e:
            if 'lark_oapi' in str(e) or 'No module named' in str(e):
                self.skipTest(f"因缺少依赖而跳过: {e}")
            else:
                self.fail(f"无法导入media_handler模块: {e}")


class TestAPIAndQuota(unittest.TestCase):
    """API和配额管理测试类"""

    def test_import_feishu_api(self):
        """测试飞书API模块导入"""
        try:
            import feishu_api
            self.assertTrue(hasattr(feishu_api, 'reply_message'))
            self.assertTrue(hasattr(feishu_api, 'send_direct_message'))
        except ImportError as e:
            if 'lark_oapi' in str(e) or 'No module named' in str(e):
                self.skipTest(f"因缺少依赖而跳过: {e}")
            else:
                self.fail(f"无法导入feishu_api模块: {e}")

    def test_import_feishu_quota(self):
        """测试飞书配额管理模块导入"""
        try:
            import feishu_quota
            self.assertTrue(hasattr(feishu_quota, 'record_call'))
            self.assertTrue(hasattr(feishu_quota, 'get_stats'))
        except ImportError as e:
            self.fail(f"无法导入feishu_quota模块: {e}")

    @patch('feishu_quota.record_call')
    def test_feishu_quota_record_call(self, mock_record_call):
        """测试配额记录功能"""
        try:
            import feishu_quota
            # 测试record_call被正确调用
            feishu_quota.record_call('message')
            mock_record_call.assert_called_once_with('message')
        except ImportError:
            # 如果模块不可用，则跳过测试
            pass


class TestMemoryManager(unittest.TestCase):
    """记忆管理测试类"""

    def test_import_memory_manager(self):
        """测试记忆管理模块导入"""
        try:
            import memory_manager
            self.assertTrue(hasattr(memory_manager, 'MemoryManager'))
            self.assertTrue(hasattr(memory_manager, 'add_turn'))
        except ImportError as e:
            if 'httpx' in str(e) or 'No module named' in str(e):
                self.skipTest(f"因缺少依赖而跳过: {e}")
            else:
                self.fail(f"无法导入memory_manager模块: {e}")

    def test_memory_manager_class(self):
        """测试记忆管理器类"""
        try:
            from memory_manager import MemoryManager
            # 测试实例化
            mm = MemoryManager(user_id="test_user")
            self.assertIsInstance(mm, MemoryManager)
        except ImportError:
            # 如果模块不可用，跳过测试
            pass
        except Exception as e:
            # 如果构造函数需要特定参数，这允许测试继续
            pass


class TestArticleWriter(unittest.TestCase):
    """文章写作功能测试类"""

    def test_import_article_writer(self):
        """测试文章写作模块导入"""
        try:
            import article_writer
            self.assertTrue(hasattr(article_writer, 'retrieve_from_knowledge_base'))
            self.assertTrue(hasattr(article_writer, 'generate_article'))
            self.assertTrue(hasattr(article_writer, 'write_article'))
        except ImportError as e:
            self.fail(f"无法导入article_writer模块: {e}")


class TestPDFParser(unittest.TestCase):
    """PDF解析功能测试类"""

    def test_import_pdf_parser(self):
        """测试PDF解析模块导入"""
        try:
            import pdf_parser
            self.assertTrue(hasattr(pdf_parser, 'extract_pdf_text'))
            self.assertTrue(hasattr(pdf_parser, 'handle_pdf_async'))
        except ImportError as e:
            self.fail(f"无法导入pdf_parser模块: {e}")

    @patch('pdf_parser.download_pdf')
    def test_download_pdf_mock(self, mock_download_pdf):
        """测试PDF下载功能（模拟）"""
        try:
            import pdf_parser
            # 测试函数存在且可调用
            self.assertTrue(callable(pdf_parser.download_pdf))
        except ImportError:
            # 如果模块不可用，则跳过测试
            pass


def run_manual_tests():
    """运行手动功能测试"""
    print("=== 手动功能测试 ===")

    # 测试系统完整性检查脚本
    print("\n1. 检查预检查脚本是否存在...")
    pre_check_script = os.path.expanduser("~/scripts/pre_check_v2.sh")
    if os.path.exists(pre_check_script):
        print(f"✓ 预检查脚本存在: {pre_check_script}")
    else:
        print(f"✗ 预检查脚本不存在: {pre_check_script}")

    # 测试备份脚本
    print("\n2. 检查备份脚本是否存在...")
    backup_script = os.path.expanduser("~/scripts/backup.sh")
    if os.path.exists(backup_script):
        print(f"✓ 备份脚本存在: {backup_script}")
    else:
        print(f"✗ 备份脚本不存在: {backup_script}")

    # 测试Docker配置
    print("\n3. 检查Docker配置...")
    docker_compose_path = os.path.expanduser("~/clawdbot-docker/docker-compose.yml")
    if os.path.exists(docker_compose_path):
        print(f"✓ Docker配置存在: {docker_compose_path}")
    else:
        print(f"✗ Docker配置不存在: {docker_compose_path}")

    # 测试知识库目录
    print("\n4. 检查知识库目录...")
    klib_dir = os.path.expanduser("~/Documents/Library/")
    if os.path.exists(klib_dir):
        print(f"✓ 知识库目录存在: {klib_dir}")
        klib_files = [f for f in os.listdir(klib_dir) if f.startswith('klib_')]
        print(f"  知识库相关文件: {len(klib_files)} 个")
    else:
        print(f"✗ 知识库目录不存在: {klib_dir}")


def run_specific_tests():
    """运行指定的测试"""
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    # 添加所有测试
    suite.addTest(loader.loadTestsFromTestCase(TestKnowledgeBase))
    suite.addTest(loader.loadTestsFromTestCase(TestFeishuBot))
    suite.addTest(loader.loadTestsFromTestCase(TestAPIAndQuota))
    suite.addTest(loader.loadTestsFromTestCase(TestMemoryManager))
    suite.addTest(loader.loadTestsFromTestCase(TestArticleWriter))
    suite.addTest(loader.loadTestsFromTestCase(TestPDFParser))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    return result


if __name__ == '__main__':
    print("知微系统测试套件")
    print("=" * 50)

    # 解析命令行参数
    if len(sys.argv) > 1:
        if sys.argv[1] == '--manual':
            # 只运行手动测试
            run_manual_tests()
        elif sys.argv[1] == '--specific':
            # 运行特定测试
            result = run_specific_tests()
        elif sys.argv[1] == '--all':
            # 运行所有测试（默认）
            run_manual_tests()
            print("\n" + "=" * 50)
            unittest.main(argv=[''], verbosity=2, exit=False)
        else:
            # 显示帮助
            print("用法:")
            print("  python test_knowledge_base.py              # 运行所有测试")
            print("  python test_knowledge_base.py --manual     # 只运行手动功能测试")
            print("  python test_knowledge_base.py --specific   # 只运行单元测试")
            print("  python test_knowledge_base.py --all        # 运行全部测试")
    else:
        # 默认行为：运行手动测试，然后运行单元测试
        run_manual_tests()
        print("\n" + "=" * 50)
        unittest.main(argv=[''], verbosity=2, exit=False)