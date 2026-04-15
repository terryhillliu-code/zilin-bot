#!/usr/bin/env python3
"""
记忆系统单元测试

运行：
    pytest test_memory_manager.py -v
"""
import pytest
from memory_manager import (
    extract_important_info,
    extract_important_info_enhanced,
    MemoryVector,
    MemoryVectorStore,
    MemoryManager,
)


class TestExtractImportantInfo:
    """测试记忆提取函数"""

    def test_preference_basic(self):
        """测试基本偏好提取"""
        result = extract_important_info("我喜欢简洁的回答", "好的")
        assert result is not None
        assert result["key"] == "用户偏好"
        assert "简洁" in result["value"]

    def test_preference_none(self):
        """测试无偏好信息"""
        result = extract_important_info("你好", "你好！有什么可以帮你？")
        assert result is None

    def test_task_completion(self):
        """测试任务完成提取"""
        result = extract_important_info("", "任务已完成，请查看结果")
        assert result is not None
        assert result["key"] == "完成任务"

    def test_decision(self):
        """测试决策记录提取"""
        result = extract_important_info("我决定使用 Python 开发", "好的选择")
        assert result is not None
        assert result["key"] == "决策记录"


class TestExtractImportantInfoEnhanced:
    """测试增强版记忆提取"""

    def test_preference_extended_keywords(self):
        """测试扩展偏好关键词"""
        result = extract_important_info_enhanced("我一般用 VS Code 编辑器", "好的")
        assert result is not None
        assert result["key"] == "用户偏好"

    def test_preference_negative(self):
        """测试负面偏好"""
        result = extract_important_info_enhanced("我不喜欢冗长的回答", "我会简洁回答")
        assert result is not None
        assert result["key"] == "用户偏好"

    def test_technology_stack(self):
        """测试技术栈提取"""
        result = extract_important_info_enhanced(
            "我基于 Django 框架开发 Web 应用", "Django 是很好的选择"
        )
        assert result is not None
        assert result["key"] == "技术栈"

    def test_short_message_no_extraction(self):
        """测试短消息不触发 LLM 提取"""
        result = extract_important_info_enhanced("你好", "你好")
        assert result is None


class TestMemoryVector:
    """测试记忆向量数据结构"""

    def test_memory_vector_creation(self):
        """测试 MemoryVector 创建"""
        memory = MemoryVector(
            id="test-001",
            user_id="test-user",
            text="测试文本",
            user_msg="用户消息",
            assistant_msg="助手回复",
            memory_type="conversation",
            timestamp="2026-01-01",
        )
        assert memory.id == "test-001"
        assert memory.vector == []  # 默认空列表

    def test_memory_vector_with_vector(self):
        """测试带向量的 MemoryVector"""
        memory = MemoryVector(
            id="test-002",
            user_id="test-user",
            text="测试文本",
            user_msg="用户消息",
            assistant_msg="助手回复",
            memory_type="preference",
            timestamp="2026-01-01",
            vector=[0.1, 0.2, 0.3],
        )
        assert memory.vector == [0.1, 0.2, 0.3]


class TestMemoryVectorStore:
    """测试记忆向量存储"""

    def test_validate_user_id_valid(self):
        """测试有效的 user_id"""
        store = MemoryVectorStore.__new__(MemoryVectorStore)
        safe_id = store._validate_user_id("ou_abc123")
        assert safe_id == "ou_abc123"

    def test_validate_user_id_with_dash(self):
        """测试带横线的 user_id"""
        store = MemoryVectorStore.__new__(MemoryVectorStore)
        safe_id = store._validate_user_id("test-user-001")
        assert safe_id == "test-user-001"

    def test_validate_user_id_invalid(self):
        """测试无效的 user_id（含特殊字符）"""
        store = MemoryVectorStore.__new__(MemoryVectorStore)
        with pytest.raises(ValueError):
            store._validate_user_id("user;DROP TABLE")

    def test_validate_user_id_with_quote(self):
        """测试带单引号的 user_id"""
        store = MemoryVectorStore.__new__(MemoryVectorStore)
        with pytest.raises(ValueError):
            store._validate_user_id("user'123")


class TestMemoryManager:
    """测试记忆管理器"""

    def test_build_context_prompt_empty(self):
        """测试空记忆时的 context prompt"""
        mm = MemoryManager("test-user-empty", max_working_rounds=6, enable_vector=False)
        prompt = mm.build_context_prompt()
        assert prompt == ""

    def test_build_context_prompt_with_working_memory(self):
        """测试有工作记忆时的 context prompt"""
        mm = MemoryManager("test-user-working", max_working_rounds=6, enable_vector=False)
        mm.add_turn("你好", "你好！")
        prompt = mm.build_context_prompt()
        assert "最近对话" in prompt

    def test_add_turn_with_vector_disabled(self):
        """测试禁用向量存储时的 add_turn"""
        mm = MemoryManager("test-user-disable", max_working_rounds=6, enable_vector=False)
        mm.working_memory = []  # 清空确保初始状态
        mm.add_turn("我喜欢简洁的回答", "好的")
        assert len(mm.working_memory) == 1

    def test_get_stats(self):
        """测试统计信息"""
        mm = MemoryManager("test-user-stats", max_working_rounds=6, enable_vector=False)
        stats = mm.get_stats()
        assert "工作记忆" in stats
        assert "摘要" in stats

    def test_reset(self):
        """测试重置记忆"""
        mm = MemoryManager("test-user-reset", max_working_rounds=6, enable_vector=False)
        mm.add_turn("你好", "你好！")
        mm.reset()
        assert len(mm.working_memory) == 0

    def test_compress_with_anchor_extraction(self):
        """测试压缩时锚点提取（模拟，不实际调用 LLM）"""
        mm = MemoryManager("test-user-compress", max_working_rounds=2, enable_vector=False)
        # 添加足够多的轮次触发压缩
        mm.add_turn("我喜欢简洁的回答", "好的，我会简洁回答")
        mm.add_turn("我决定用 React 开发", "好的选择")
        mm.add_turn("任务已完成", "很好")
        # 验证压缩后被移除
        assert len(mm.working_memory) <= 2

    def test_extract_anchor_info_mock(self):
        """测试锚点提取函数（模拟输入）"""
        mm = MemoryManager("test-anchor-mock", max_working_rounds=6, enable_vector=False)
        # 模拟锚点数据
        mock_anchors = [
            {"key": "偏好_0415", "value": "喜欢简洁的回答"},
            {"key": "决策_0415", "value": "用React开发"},
        ]
        for anchor in mock_anchors:
            mm.save_persistent(anchor["key"], anchor["value"])
        # 验证持久记忆
        persistent = mm.get_persistent()
        assert any("简洁" in str(v) for v in persistent.values())


if __name__ == "__main__":
    pytest.main([__file__, "-v"])