#!/usr/bin/env python3
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# 加载环境
load_dotenv("/Users/liufang/.secrets/global.env")
api_key = os.getenv("DASHSCOPE_API_KEY")

# 引入 distiller 组件
sys.path.insert(0, "/Users/liufang/zhiwei-bot/scripts")
from douyin_distiller import DashScopeASRTranscriber, TranscriptResult

def test_asr():
    transcriber = DashScopeASRTranscriber(api_key)
    
    # 构建测试音频 (如果环境中有旧音频则复用，否则生成静音文件)
    # 注意：Paraformer 如果全是静音可能会返回空，这正好可以测试空处理
    test_audio = Path("/tmp/test_asr.mp3")
    if not test_audio.exists():
        import subprocess
        subprocess.run(["ffmpeg", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "5", str(test_audio)], check=True)
    
    print(f"Testing ASR with: {test_audio}")
    try:
        result = transcriber.transcribe(test_audio)
        print(f"Status: SUCCESS" if result.full_text else "Status: EMPTY_RESULT")
        print(f"Text: {result.full_text}")
        print(f"Source: {result.source}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    if not api_key:
        print("Error: DASHSCOPE_API_KEY not found")
        sys.exit(1)
    test_asr()
