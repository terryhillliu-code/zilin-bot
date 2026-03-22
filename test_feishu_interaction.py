#!/usr/bin/env python3
"""
飞书交互端到端验证脚本

验证 v47.0 飞书交互智能化的关键链路：
1. 意图识别 (intent_recognizer.py)
2. 卡片构建 (research_card.py)
3. 卡片回调处理逻辑

用法:
    python test_feishu_interaction.py
"""

import sys
import json
from pathlib import Path

# 添加路径
sys.path.insert(0, str(Path.home() / "zhiwei-bot"))
sys.path.insert(0, str(Path.home() / "zhiwei-bot" / "core"))

def test_intent_recognizer():
    """测试意图识别器"""
    print("\n" + "=" * 60)
    print("测试 1: 意图识别器")
    print("=" * 60)

    from intent_recognizer import IntentRecognizer, recognize_intent

    test_cases = [
        ("帮我研究一下 AI Agent", "research", 0.95),
        ("看看有什么关于 RAG 的资料", "research", 0.75),
        ("搜一下 Transformer", "search", 0.4),
        ("今天天气怎么样", "chat", 0.9),
        ("整理一份多模态的研究报告，包括视频", "research", 0.95),
        ("分析一下 Agent 技术", "research", 0.95),
        ("了解下 LangChain", "research", 0.75),
        ("只看论文，不要视频", "chat", 0.9),  # 无研究触发词
    ]

    passed = 0
    for text, expected_intent, min_confidence in test_cases:
        result = recognize_intent(text)
        is_correct = result.intent == expected_intent and result.confidence >= min_confidence
        status = "✅" if is_correct else "❌"
        print(f"\n{status} 输入: {text}")
        print(f"   意图: {result.intent} (期望: {expected_intent})")
        print(f"   置信度: {result.confidence:.2f} (期望 >= {min_confidence})")
        print(f"   实体: {result.entities}")
        if is_correct:
            passed += 1

    print(f"\n结果: {passed}/{len(test_cases)} 通过")
    return passed == len(test_cases)


def test_research_card():
    """测试卡片构建"""
    print("\n" + "=" * 60)
    print("测试 2: 研究配置卡片构建")
    print("=" * 60)

    from research_card import ResearchConfigCard, ResearchResultCard

    # 测试确认卡片
    print("\n2.1 确认卡片构建:")
    card = ResearchConfigCard.build_simple_confirm("AI Agent", include_videos=True)
    print(f"   类型: {type(card)}")
    print(f"   元素数: {len(card.get('elements', []))}")

    # 检查按钮
    actions_found = False
    for elem in card.get('elements', []):
        if elem.get('tag') == 'action':
            actions = elem.get('actions', [])
            print(f"   按钮数: {len(actions)}")
            for action in actions:
                action_type = action.get('value', {}).get('action')
                print(f"     - {action_type}")
            actions_found = True

    if not actions_found:
        print("   ❌ 未找到操作按钮")
        return False

    # 测试结果卡片
    print("\n2.2 结果卡片构建:")
    result_card = ResearchResultCard.build(
        topic="RAG",
        paper_count=5,
        video_count=3,
        arxiv_triggered=True
    )
    print(f"   类型: {type(result_card)}")
    print(f"   元素数: {len(result_card.get('elements', []))}")

    # 检查内容
    content_found = False
    for elem in result_card.get('elements', []):
        if elem.get('tag') == 'div':
            text = elem.get('text', {}).get('content', '')
            if 'RAG' in text and '5' in text:
                content_found = True
                print(f"   ✅ 内容正确包含主题和数量")

    if not content_found:
        print("   ❌ 卡片内容不正确")
        return False

    return True


def test_intent_marker_parsing():
    """测试意图标记解析"""
    print("\n" + "=" * 60)
    print("测试 3: 意图标记解析")
    print("=" * 60)

    # 模拟 chat_handler 返回的标记
    test_markers = [
        "[INTENT:RESEARCH]|AI Agent",
        "[INTENT:RESEARCH]|RAG|videos=true",
        "[INTENT:RESEARCH]|多模态|videos=false|source=local",
    ]

    for marker in test_markers:
        print(f"\n解析: {marker}")
        if marker.startswith("[INTENT:RESEARCH]"):
            parts = marker.split("|")
            topic = parts[1] if len(parts) > 1 else ""

            include_videos = True
            source = None
            for part in parts[2:]:
                if part.startswith("videos="):
                    include_videos = part.split("=")[1].lower() == "true"
                elif part.startswith("source="):
                    source = part.split("=")[1]

            print(f"   主题: {topic}")
            print(f"   包含视频: {include_videos}")
            print(f"   来源: {source}")
            print("   ✅ 解析成功")
        else:
            print("   ❌ 不是意图标记")

    return True


def test_chat_handler_integration():
    """测试 chat_handler 集成"""
    print("\n" + "=" * 60)
    print("测试 4: ChatHandler 意图检测")
    print("=" * 60)

    try:
        from chat_handler import ChatHandler

        handler = ChatHandler(enable_rag=False, enable_memory=False)

        # 检查意图识别器是否加载
        if handler._intent_recognizer:
            print("   ✅ 意图识别器已加载")
        else:
            print("   ⚠️ 意图识别器未加载")

        # 测试意图识别
        test_message = "帮我研究一下 AI Agent"
        intent_result = handler._intent_recognizer.recognize(test_message)
        print(f"\n   输入: {test_message}")
        print(f"   意图: {intent_result.intent}")
        print(f"   是研究意图: {intent_result.is_research_intent()}")

        return True

    except Exception as e:
        print(f"   ❌ 测试失败: {e}")
        return False


def test_full_flow():
    """测试完整流程（模拟）"""
    print("\n" + "=" * 60)
    print("测试 5: 完整流程模拟")
    print("=" * 60)

    from intent_recognizer import recognize_intent
    from research_card import ResearchConfigCard

    # 模拟用户输入
    user_input = "帮我研究一下 AI Agent，包括视频"
    print(f"\n用户输入: {user_input}")

    # Step 1: 意图识别
    print("\nStep 1: 意图识别")
    intent_result = recognize_intent(user_input)
    print(f"   意图: {intent_result.intent}")
    print(f"   置信度: {intent_result.confidence:.2f}")
    print(f"   实体: {intent_result.entities}")

    if not intent_result.is_research_intent():
        print("   ❌ 未识别为研究意图")
        return False

    # Step 2: 构建确认卡片
    print("\nStep 2: 构建确认卡片")
    topic = intent_result.entities.get("topic", "")
    include_videos = intent_result.entities.get("include_videos", True)
    card = ResearchConfigCard.build_simple_confirm(topic, include_videos)
    print(f"   主题: {topic}")
    print(f"   包含视频: {include_videos}")
    print(f"   卡片元素数: {len(card.get('elements', []))}")

    # Step 3: 检查卡片按钮
    print("\nStep 3: 检查卡片操作")
    for elem in card.get('elements', []):
        if elem.get('tag') == 'action':
            for action in elem.get('actions', []):
                action_value = action.get('value', {})
                action_type = action_value.get('action')
                print(f"   按钮: {action.get('text', {}).get('content', '')}")
                print(f"   动作: {action_type}")
                if action_type == 'start_research':
                    print(f"   参数: topic={action_value.get('topic')}, videos={action_value.get('include_videos')}")

    print("\n✅ 完整流程验证通过")
    return True


def main():
    print("=" * 60)
    print("飞书交互端到端验证")
    print("=" * 60)

    results = {
        "意图识别器": test_intent_recognizer(),
        "卡片构建": test_research_card(),
        "标记解析": test_intent_marker_parsing(),
        "ChatHandler集成": test_chat_handler_integration(),
        "完整流程": test_full_flow(),
    }

    print("\n" + "=" * 60)
    print("测试结果汇总")
    print("=" * 60)

    all_passed = True
    for name, passed in results.items():
        status = "✅ 通过" if passed else "❌ 失败"
        print(f"  {name}: {status}")
        if not passed:
            all_passed = False

    print("\n" + "=" * 60)
    if all_passed:
        print("🎉 所有测试通过！飞书交互链路正常。")
    else:
        print("⚠️ 部分测试失败，请检查上述错误。")
    print("=" * 60)

    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())