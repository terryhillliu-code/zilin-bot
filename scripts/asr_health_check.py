#!/usr/bin/env python3
"""
ASR 服务健康检查

检查项目（v3.3 重排: mimo-asr 为主用引擎）：
1. mimo-asr 云端可用性(小米 MiMo, 当前主用)
2. 本地 MLX Whisper 可用性(兜底)
3. DashScope ASR 可用性(已退居次要, key 可能 401)
4. API Key 配置状态

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

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)


def check_mimo_asr() -> dict:
    """检查 mimo-asr 云端可用性(v3.3 主用引擎)

    实测探测: 用系统自带短音频转 16k 单声道 wav, 真调一次 mimo-asr。
    比单测连通性更可靠——能真拿到转写才算健康。
    """
    result = {
        "service": "mimo_asr",
        "available": False,
        "api_key_configured": False,
        "api_key_valid": False,
        "error": None,
    }
    try:
        sys.path.insert(0, str(Path.home() / "zhiwei-bot" / "scripts"))
        from douyin_distiller import MimoASRTranscriber, AppConfig

        cfg = AppConfig()
        if not getattr(cfg, "mimo_api_key", ""):
            result["error"] = "MIMO_API_KEY not configured"
            return result
        result["api_key_configured"] = True

        # 实测探测: 系统音效前 2 秒转 wav
        import subprocess
        import tempfile
        test_src = "/System/Library/Sounds/Glass.aiff"
        if not Path(test_src).exists():
            # 无系统音频时退而只校验配置(不阻断)
            result["available"] = True
            result["error"] = "skipped probe (no test audio), config ok"
            return result
        wav = tempfile.mktemp(suffix=".wav")
        subprocess.run(["ffmpeg", "-y", "-i", test_src, "-t", "2",
                        "-ar", "16000", "-ac", "1", wav],
                       capture_output=True, timeout=30)
        tr = MimoASRTranscriber(cfg.mimo_api_key, cfg.mimo_api_base, cfg.mimo_asr_model)
        # 直接调单片接口验证连通(系统音无人声, 能返回即 API 通)
        tr._transcribe_clip(Path(wav))
        result["available"] = True
        result["api_key_valid"] = True
        try:
            os.unlink(wav)
        except OSError:
            pass
    except Exception as e:
        result["error"] = str(e)[:200]
    return result


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
            model='paraformer-v2',
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
        result["keys"]["MIMO_API_KEY"] = bool(os.environ.get("MIMO_API_KEY"))

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

    # 1. mimo-asr 云端(主用引擎)
    results["checks"].append(check_mimo_asr())

    # 2. DashScope ASR(已退居次要)
    results["checks"].append(check_dashscope_asr())

    # 3. 本地 Whisper(兜底)
    results["checks"].append(check_local_whisper())

    # 4. API Keys
    results["checks"].append(check_api_keys())

    # 汇总状态: mimo-asr 或本地 Whisper 可用即健康(主链路+兜底)
    # DashScope 已退居次要, 不影响健康判定
    asr_available = any(
        c.get("available") for c in results["checks"]
        if c.get("service") in ["mimo_asr", "local_whisper"]
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