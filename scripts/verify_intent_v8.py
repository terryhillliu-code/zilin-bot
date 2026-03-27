#!/usr/bin/env python3
import sys
from pathlib import Path

# 设置环境
sys.path.insert(0, "/Users/liufang/zhiwei-bot")
from core.intent_recognizer import IntentRecognizer

def test_json_intent():
    print("Testing JSON Intent Parsing...")
    recognizer = IntentRecognizer()
    json_input = """
    好的，我理解了。
    ```json
    {
      "intent": "research",
      "topic": "HBM3 内存架构",
      "confidence": 0.98,
      "reasoning": "用户询问 HBM3 技术细节，命中深度研究意图",
      "entities": {
        "include_videos": true,
        "source": "online"
      }
    }
    ```
    请确认是否启动。
    """
    result = recognizer.recognize(json_input)
    print(f"Intent: {result.intent}")
    print(f"Topic: {result.entities.get('topic')}")
    print(f"Reasoning: {result.reasoning}")
    print(f"Confidence: {result.confidence}")
    
    assert result.intent == "research"
    assert result.entities.get("topic") == "HBM3 内存架构"
    print("✅ JSON Parsing Verified.")

def test_legacy_intent():
    print("\nTesting Legacy Intent Parsing...")
    recognizer = IntentRecognizer()
    legacy_input = "[INTENT:RESEARCH]|CXL 3.0|false|local"
    result = recognizer.recognize(legacy_input)
    print(f"Intent: {result.intent}")
    print(f"Topic: {result.entities.get('topic')}")
    
    assert result.intent == "research"
    assert result.entities.get("topic") == "CXL 3.0"
    assert result.entities.get("include_videos") is False
    print("✅ Legacy Parsing Verified.")

if __name__ == "__main__":
    try:
        test_json_intent()
        test_legacy_intent()
    except Exception as e:
        print(f"❌ Test Failed: {e}")
