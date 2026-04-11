#!/usr/bin/env python3
"""
ASR 服务健康检查

检查项目：
1. DashScope ASR API 可用性
2. 本地 MLX Whisper 可用性
3. API Key 配置状态

用法：
    python scripts/asr_health_check.py
    python scripts/asr_health_check.py --json
"""

import os
import sys
import json
import logging
from pathlib import Path
from datetime import datetime

# 添加路径
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path.home() / "zhiwei-common"))

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)


def check_dashscope_asr() -> dict:
    """检查 DashScope ASR 可用性"""
    result = {
        "service": "dashscope_asr",
        "available": False,
        "api_key_configured": False,
        "api_key_valid": False,
        "error": None
    }

    try:
        from zhiwei_common import get_asr_key

        api_key = get_asr_key()
        if api_key:
            result["api_key_configured"] = True
        else:
            result["error"] = "DASHSCOPE_API_KEY not configured"
            return result

        # 测试 API 调用
        import dashscope
        dashscope.api_key = api_key

        from dashscope.audio.asr import Recognition

        # 使用系统音频测试
        test_audio = '/System/Library/Sounds/Glass.aiff'

        class SilentCallback:
            def on_result(self, result): pass
            def on_error(self, error): pass

        recognition = Recognition(
            model='paraformer-realtime-v2',
            format='aiff',
            sample_rate=44100,
            callback=SilentCallback()
        )

        response = recognition.call(file=test_audio)

        if response.status_code == 200:
            result["available"] = True
            result["api_key_valid"] = True
        else:
            result["error"] = f"API returned {response.status_code}: {response.message}"

    except ImportError as e:
        result["error"] = f"dashscope not installed: {e}"
    except Exception as e:
        result["error"] = str(e)[:200]

    return result


def check_local_whisper() -> dict:
    """检查本地 MLX Whisper 可用性"""
    result = {
        "service": "local_whisper",
        "available": False,
        "model": "small",
        "error": None
    }

    try:
        import mlx_whisper

        # 检查模型是否存在
        model_name = os.environ.get("LOCAL_ASR_MODEL", "small")
        result["model"] = model_name
        result["available"] = True

    except ImportError:
        result["error"] = "mlx-whisper not installed. Run: pip install mlx-whisper"
    except Exception as e:
        result["error"] = str(e)[:200]

    return result


def check_api_keys() -> dict:
    """检查 API Key 配置状态"""
    result = {
        "check": "api_keys",
        "keys": {}
    }

    try:
        from zhiwei_common import get_asr_key, get_llm_key

        asr_key = get_asr_key()
        result["keys"]["DASHSCOPE_API_KEY"] = bool(asr_key)

        llm_key = get_llm_key()
        result["keys"]["LLM_KEY"] = bool(llm_key)

        # 检查 key 来源
        result["keys"]["BAILIAN_API_KEY"] = bool(os.environ.get("BAILIAN_API_KEY"))
        result["keys"]["CODING_PLAN_API_KEY"] = bool(os.environ.get("CODING_PLAN_API_KEY"))

    except Exception as e:
        result["error"] = str(e)

    return result


def run_health_check(json_output: bool = False) -> dict:
    """运行完整健康检查"""
    results = {
        "timestamp": datetime.now().isoformat(),
        "checks": []
    }

    # 1. DashScope ASR
    results["checks"].append(check_dashscope_asr())

    # 2. 本地 Whisper
    results["checks"].append(check_local_whisper())

    # 3. API Keys
    results["checks"].append(check_api_keys())

    # 汇总状态
    asr_available = any(
        c.get("available") for c in results["checks"]
        if c.get("service") in ["dashscope_asr", "local_whisper"]
    )
    results["status"] = "healthy" if asr_available else "degraded"

    if json_output:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        print_report(results)

    return results


def print_report(results: dict):
    """打印健康检查报告"""
    print("=" * 50)
    print("ASR 服务健康检查报告")
    print(f"时间: {results['timestamp']}")
    print("=" * 50)

    for check in results["checks"]:
        if "service" in check:
            status = "✅" if check.get("available") else "❌"
            print(f"\n{status} {check['service']}")

            if check.get("api_key_configured") is not None:
                key_status = "✅" if check["api_key_configured"] else "❌"
                print(f"   API Key: {key_status}")

            if check.get("api_key_valid") is not None:
                valid_status = "✅" if check["api_key_valid"] else "❌"
                print(f"   API Valid: {valid_status}")

            if check.get("model"):
                print(f"   Model: {check['model']}")

            if check.get("error"):
                print(f"   Error: {check['error']}")

        elif "keys" in check:
            print("\n📋 API Key 状态:")
            for key, configured in check["keys"].items():
                status = "✅" if configured else "❌"
                print(f"   {status} {key}")

    print("\n" + "=" * 50)
    status = results["status"].upper()
    if status == "HEALTHY":
        print(f"状态: 🟢 {status}")
    else:
        print(f"状态: 🟡 {status}")
    print("=" * 50)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="ASR 服务健康检查")
    parser.add_argument("--json", action="store_true", help="输出 JSON 格式")
    args = parser.parse_args()

    run_health_check(json_output=args.json)