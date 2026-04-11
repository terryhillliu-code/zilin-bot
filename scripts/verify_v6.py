#!/usr/bin/env python3
import os
import sys
from pathlib import Path

# 设置环境
sys.path.insert(0, "/Users/liufang/zhiwei-bot")
import media_handler

# Mock 依赖
class MockTaskLogger:
    @staticmethod
    def log_task(*args): pass

media_handler.TaskLogger = MockTaskLogger

def test_asr_redirection():
    print("Testing ASR Redirection (media_handler -> douyin_distiller)...")
    # 这里我们只验证导入和函数签名，不实际调用 API（节省额度）
    try:
        from douyin_distiller import DashScopeASRTranscriber
        print("✅ DashScopeASRTranscriber found in distiller.")
    except Exception as e:
        print(f"❌ Transcriber import failed: {e}")

def test_url_redirection():
    print("\nTesting URL Redirection (media_handler -> url_ingest)...")
    test_url = "https://example.com"
    # 这里我们通过打印 log 来确认逻辑
    try:
        result = media_handler.summarize_url(test_url)
        print(f"Result (Mocked/Error): {result}")
    except Exception as e:
        print(f"❌ URL summarize exception: {e}")

if __name__ == "__main__":
    test_asr_redirection()
    test_url_redirection()
