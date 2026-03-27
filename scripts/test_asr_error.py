#!/usr/bin/env python3
import os
import sys
from pathlib import Path

# 引入 distiller 组件
sys.path.insert(0, "/Users/liufang/zhiwei-bot/scripts")
from douyin_distiller import DashScopeASRTranscriber, TranscriptResult

def test_error_propagation():
    # 使用错误的 API Key 模拟 401 Unauthorized
    invalid_key = "sk-invalid-key-testing"
    transcriber = DashScopeASRTranscriber(invalid_key)
    
    test_audio = Path("/tmp/test_asr.mp3")
    if not test_audio.exists():
        import subprocess
        subprocess.run(["ffmpeg", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "1", str(test_audio)], check=True)
    
    print(f"Testing Error Propagation with invalid key...")
    try:
        result = transcriber.transcribe(test_audio)
        print(f"Result Full Text: '{result.full_text}'")
        print(f"Result Error Details: '{result.error_details}'")
        
        # 验证逻辑：应该包含 API Error 信息
        if "DashScope API Error" in result.error_details and "Unauthorized" in result.error_details:
             print("\n✅ Verification SUCCESS: Detailed error correctly captured!")
        else:
             print("\n❌ Verification FAILED: Error message not as expected.")
             
    except Exception as e:
        print(f"Unexpected Exception: {e}")

if __name__ == "__main__":
    test_error_propagation()
