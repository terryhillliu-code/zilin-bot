#!/usr/bin/env python3
"""
Mimo TTS 客户端
调用小米 Mimo v2.5-tts 系列模型实现文字转语音。

使用模式：
  python mimo_tts.py "你好世界" -o output.mp3

环境变量：
  MIMO_API_KEY - API 密钥
  MIMO_API_BASE - API 基础地址（默认 https://api.mimo.com/v1）
  MIMO_TTS_MODEL - TTS 模型名（默认 mimo-v2.5-tts）
"""

import os
import sys
import json
import time
import logging
from pathlib import Path
from typing import Optional
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TTSConfig:
    """TTS 配置"""
    model: str = "mimo-v2.5-tts"
    voice_id: str = "default"
    speed: float = 1.0
    volume: float = 1.0
    sample_rate: int = 24000
    format: str = "mp3"
    channel: int = 1
    bitrate: int = 128000
    timeout: int = 120


class MimoTTSClient:
    """Mimo TTS 客户端"""

    def __init__(self, api_key: Optional[str] = None, config: Optional[TTSConfig] = None):
        self.api_key = api_key or os.getenv("MIMO_API_KEY", "")
        self.config = config or TTSConfig()

        # API 基础地址，支持自定义
        api_base = os.getenv("MIMO_API_BASE")
        if not api_base:
            api_base = "https://api.mimo.com/v1"
        self.api_base = api_base

        # 模型名，支持环境变量覆盖
        self.model = os.getenv("MIMO_TTS_MODEL", self.config.model)

    def is_available(self) -> bool:
        """检查 TTS 服务是否可用"""
        return bool(self.api_key)

    def synthesize(
        self,
        text: str,
        voice_id: Optional[str] = None,
        speed: Optional[float] = None,
        output_path: Optional[str] = None,
    ) -> Optional[str]:
        """将文字转换为语音

        Args:
            text: 要转换的文本
            voice_id: 音色 ID（可选，使用默认音色）
            speed: 语速（0.5-2.0，可选）
            output_path: 输出文件路径（可选，自动生成临时文件）

        Returns:
            生成的音频文件路径，失败返回 None
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
            output_path = str(tmp_dir / f"tts_{int(time.time())}.mp3")

        # 构建请求
        voice_setting = {"voice_id": voice_id or self.config.voice_id}
        if speed is not None:
            voice_setting["speed"] = speed

        payload = {
            "model": self.model,
            "text": text.strip(),
            "stream": False,
            "voice_setting": voice_setting,
            "audio_setting": {
                "sample_rate": self.config.sample_rate,
                "bitrate": self.config.bitrate,
                "format": self.config.format,
                "channel": self.config.channel,
            },
        }

        try:
            url = f"{self.api_base.rstrip('/')}/audio/speech"
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }

            logger.info(f"🔊 调用 Mimo TTS: model={self.model}, text={text[:30]}...")

            response = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=self.config.timeout,
            )

            if response.status_code != 200:
                logger.error(f"❌ Mimo TTS 请求失败: {response.status_code} - {response.text[:500]}")
                return None

            # 检查响应类型
            content_type = response.headers.get("Content-Type", "")

            if "audio" in content_type or "octet-stream" in content_type:
                # 直接返回音频数据
                audio_data = response.content
                if not audio_data:
                    logger.error("❌ Mimo TTS 返回空音频数据")
                    return None

                with open(output_path, "wb") as f:
                    f.write(audio_data)

                file_size = os.path.getsize(output_path)
                logger.info(f"✅ TTS 生成成功: {output_path} ({file_size} bytes)")
                return output_path

            elif "json" in content_type:
                # JSON 响应，可能是 URL 或错误信息
                result = response.json()

                if "audio_url" in result:
                    # 下载音频
                    audio_response = requests.get(result["audio_url"], timeout=30)
                    if audio_response.status_code == 200:
                        with open(output_path, "wb") as f:
                            f.write(audio_response.content)
                        logger.info(f"✅ TTS 生成成功 (URL): {output_path}")
                        return output_path
                    else:
                        logger.error(f"❌ 音频下载失败: {audio_response.status_code}")
                        return None

                if "error" in result:
                    logger.error(f"❌ Mimo TTS 错误: {result['error']}")
                    return None

                logger.error(f"❌ 未知的 Mimo TTS 响应格式: {result}")
                return None

            else:
                logger.error(f"❌ 未知的 Mimo TTS 响应类型: {content_type}")
                return None

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
    voice_id: Optional[str] = None,
    speed: Optional[float] = None,
    cleanup_after_seconds: int = 300,
) -> Optional[str]:
    """TTS 合成并设置自动清理

    Args:
        text: 文本
        voice_id: 音色
        speed: 语速
        cleanup_after_seconds: 多少秒后自动删除临时文件（默认 5 分钟）

    Returns:
        音频文件路径
    """
    client = MimoTTSClient(api_key=api_key)
    output_path = client.synthesize(text, voice_id=voice_id, speed=speed)

    if output_path:
        # 设置自动清理
        def _cleanup(path):
            try:
                time.sleep(cleanup_after_seconds)
                if os.path.exists(path):
                    os.remove(path)
                    logger.info(f"🗑️ TTS 临时文件已清理: {path}")
            except Exception:
                pass

        import threading
        t = threading.Thread(target=_cleanup, args=(output_path,), daemon=True)
        t.start()

    return output_path


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(description="Mimo TTS 客户端")
    parser.add_argument("text", help="要转换的文本")
    parser.add_argument("-o", "--output", default="output.mp3", help="输出文件路径")
    parser.add_argument("-v", "--voice-id", default=None, help="音色 ID")
    parser.add_argument("-s", "--speed", type=float, default=None, help="语速 (0.5-2.0)")
    parser.add_argument("--model", default="mimo-v2.5-tts", help="TTS 模型名")

    args = parser.parse_args()

    if args.model:
        os.environ["MIMO_TTS_MODEL"] = args.model

    result = synthesize_and_cleanup(
        text=args.text,
        voice_id=args.voice_id,
        speed=args.speed,
    )

    if result:
        print(f"✅ 音频已保存: {result}")
        # 不自动清理，因为是用户指定的输出路径
    else:
        print("❌ TTS 失败，请查看日志")
        sys.exit(1)
