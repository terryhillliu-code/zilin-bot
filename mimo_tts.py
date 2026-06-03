#!/usr/bin/env python3
"""
Mimo TTS 客户端
调用小米 Mimo v2.5-tts 模型实现文字转语音。

API: OpenAI 兼容格式
Endpoint: https://token-plan-cn.xiaomimimo.com/v1/chat/completions
Model: mimo-v2.5-tts

Usage:
  python mimo_tts.py "你好世界" -o output.wav

Env:
  MIMO_API_KEY - API 密钥
  MIMO_API_BASE - API 基础地址（默认 https://token-plan-cn.xiaomimimo.com）
"""

import os
import sys
import json
import time
import base64
import logging
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class MimoTTSClient:
    """Mimo TTS 客户端"""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("MIMO_API_KEY", "")

        # API 基础地址
        api_base = os.getenv("MIMO_API_BASE")
        if not api_base:
            api_base = "https://token-plan-cn.xiaomimimo.com"
        self.api_base = api_base

        self.model = os.getenv("MIMO_TTS_MODEL", "mimo-v2.5-tts")

    def is_available(self) -> bool:
        """检查 TTS 服务是否可用"""
        return bool(self.api_key)

    def synthesize(
        self,
        text: str,
        output_path: Optional[str] = None,
    ) -> Optional[str]:
        """将文字转换为语音

        Args:
            text: 要转换的文本
            output_path: 输出文件路径（可选，自动生成临时文件）

        Returns:
            生成的音频文件路径（WAV 格式），失败返回 None
        """
        if not text.strip():
            logger.warning("TTS 输入文本为空")
            return None

        if not self.is_available():
            logger.error("MIMO_API_KEY 未配置")
            return None

        # 生成输出路径
        if not output_path:
            tmp_dir = Path.home() / "zhiwei-bot" / "tmp"
            tmp_dir.mkdir(exist_ok=True)
            output_path = str(tmp_dir / f"tts_{int(time.time())}.wav")

        payload = {
            "model": self.model,
            "max_tokens": 4096,
            "messages": [
                {"role": "assistant", "content": text.strip()}
            ],
        }

        try:
            url = f"{self.api_base}/v1/chat/completions"
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }

            logger.info(f"🔊 调用 Mimo TTS: model={self.model}, text={text[:30]}...")

            response = requests.post(url, headers=headers, json=payload, timeout=60)

            if response.status_code != 200:
                logger.error(f"❌ Mimo TTS 请求失败: {response.status_code} - {response.text[:500]}")
                return None

            result = response.json()
            choices = result.get("choices", [])
            if not choices:
                logger.error("❌ Mimo TTS 响应中无 choices")
                return None

            audio = choices[0].get("message", {}).get("audio")
            if not audio:
                logger.error("❌ Mimo TTS 响应中无 audio 字段")
                return None

            audio_data = audio.get("data")
            if not audio_data:
                logger.error("❌ Mimo TTS audio.data 为空")
                return None

            # 解码 base64 音频并保存
            raw_audio = base64.b64decode(audio_data)
            with open(output_path, "wb") as f:
                f.write(raw_audio)

            file_size = os.path.getsize(output_path)
            logger.info(f"✅ TTS 生成成功: {output_path} ({file_size} bytes)")
            return output_path

        except requests.exceptions.Timeout:
            logger.error("❌ Mimo TTS 请求超时")
            return None
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ Mimo TTS 网络异常: {e}")
            return None
        except Exception as e:
            logger.error(f"❌ Mimo TTS 未知异常: {e}")
            return None


def synthesize_and_cleanup(
    text: str,
    api_key: Optional[str] = None,
    cleanup_after_seconds: int = 300,
) -> Optional[str]:
    """TTS 合成并设置自动清理

    Args:
        text: 文本
        cleanup_after_seconds: 多少秒后自动删除临时文件（默认 5 分钟）

    Returns:
        音频文件路径
    """
    client = MimoTTSClient(api_key=api_key)
    output_path = client.synthesize(text)

    if output_path:
        import threading

        def _cleanup(path):
            try:
                time.sleep(cleanup_after_seconds)
                if os.path.exists(path):
                    os.remove(path)
                    logger.info(f"🗑️ TTS 临时文件已清理: {path}")
            except Exception:
                pass

        t = threading.Thread(target=_cleanup, args=(output_path,), daemon=True)
        t.start()

    return output_path


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(description="Mimo TTS 客户端")
    parser.add_argument("text", help="要转换的文本")
    parser.add_argument("-o", "--output", default=None, help="输出文件路径（默认自动生成）")

    args = parser.parse_args()

    result = synthesize_and_cleanup(text=args.text, cleanup_after_seconds=0)

    if result:
        print(f"✅ 音频已保存: {result}")
    else:
        print("❌ TTS 失败，请查看日志")
        sys.exit(1)
