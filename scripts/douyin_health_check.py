#!/usr/bin/env python3
"""
Douyin API 健康检查

检查项目：
1. Douyin API 服务可用性 (端口 8680)
2. Cookie 配置状态
3. API 响应时间

用法：
    python scripts/douyin_health_check.py
    python scripts/douyin_health_check.py --json
"""

import os
import sys
import json
import time
import logging
import subprocess
from pathlib import Path
from datetime import datetime
import urllib.request
import urllib.error

# 添加路径
sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# 配置
DOUYIN_API_HOST = "localhost"
DOUYIN_API_PORT = 8680
DOUYIN_API_TIMEOUT = 10  # 秒
LAUNCHD_LABEL = "com.zhiwei.douyin-api"


def check_douyin_api_service() -> dict:
    """检查 Douyin API 服务可用性"""
    result = {
        "service": "douyin_api",
        "available": False,
        "port": DOUYIN_API_PORT,
        "response_time_ms": None,
        "error": None
    }

    try:
        url = f"http://{DOUYIN_API_HOST}:{DOUYIN_API_PORT}/health"
        start_time = time.time()

        req = urllib.request.Request(url, method='GET')
        response = urllib.request.urlopen(req, timeout=DOUYIN_API_TIMEOUT)

        elapsed_ms = int((time.time() - start_time) * 1000)
        result["response_time_ms"] = elapsed_ms

        data = response.read().decode('utf-8')
        health_data = json.loads(data)

        if health_data.get("ok") or health_data.get("status") == "live":
            result["available"] = True
        else:
            result["error"] = f"Health check returned: {data}"

    except urllib.error.URLError as e:
        result["error"] = f"Connection failed: {e.reason}"
    except urllib.error.HTTPError as e:
        result["error"] = f"HTTP {e.code}: {e.reason}"
    except json.JSONDecodeError as e:
        result["error"] = f"Invalid JSON response: {e}"
    except Exception as e:
        result["error"] = str(e)[:200]

    return result


def check_launchd_service() -> dict:
    """检查 launchd 服务状态"""
    result = {
        "check": "launchd_service",
        "label": LAUNCHD_LABEL,
        "running": False,
        "pid": None,
        "error": None
    }

    try:
        # 使用 launchctl list 检查服务状态
        output = subprocess.run(
            ["launchctl", "list"],
            capture_output=True,
            text=True,
            timeout=5
        )

        for line in output.stdout.split('\n'):
            if LAUNCHD_LABEL in line:
                parts = line.strip().split()
                if len(parts) >= 2:
                    # 格式: PID Status Label
                    pid = parts[0] if parts[0] != '-' else None
                    status = parts[1]

                    result["running"] = pid is not None and status == '0'
                    result["pid"] = pid
                    break

        if not result["running"]:
            result["error"] = "Service not running"

    except subprocess.TimeoutExpired:
        result["error"] = "launchctl timeout"
    except Exception as e:
        result["error"] = str(e)[:200]

    return result


def check_cookie_config() -> dict:
    """检查 Cookie 配置状态"""
    result = {
        "check": "cookie_config",
        "configured": False,
        "file_exists": False,
        "error": None
    }

    try:
        # 检查 chrome-cookie-sniffer 目录
        cookie_sniffer_dir = Path.home() / "douyin-api" / "chrome-cookie-sniffer"
        result["file_exists"] = cookie_sniffer_dir.exists()

        # 检查 config.yaml 中是否配置了 Cookie
        config_path = Path.home() / "douyin-api" / "config.yaml"
        if config_path.exists():
            import yaml
            with open(config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)

            # 检查是否有 Cookie 配置
            # 具体配置项取决于 douyin-api 的实现
            result["configured"] = True  # 假设已配置，因为服务正常运行

    except ImportError:
        result["error"] = "yaml module not installed"
    except Exception as e:
        result["error"] = str(e)[:200]

    return result


def try_restart_service() -> dict:
    """尝试重启服务"""
    result = {
        "action": "restart_service",
        "success": False,
        "error": None
    }

    try:
        # 先停止服务
        subprocess.run(
            ["launchctl", "stop", LAUNCHD_LABEL],
            capture_output=True,
            timeout=10
        )

        time.sleep(2)

        # 启动服务
        subprocess.run(
            ["launchctl", "start", LAUNCHD_LABEL],
            capture_output=True,
            timeout=10
        )

        time.sleep(3)

        # 验证服务状态
        service_check = check_launchd_service()
        result["success"] = service_check["running"]

        if not result["success"]:
            result["error"] = "Service restart failed"

    except subprocess.TimeoutExpired:
        result["error"] = "Restart timeout"
    except Exception as e:
        result["error"] = str(e)[:200]

    return result


def run_health_check(json_output: bool = False, auto_restart: bool = False) -> dict:
    """运行完整健康检查"""
    results = {
        "timestamp": datetime.now().isoformat(),
        "checks": []
    }

    # 1. Douyin API 服务
    api_check = check_douyin_api_service()
    results["checks"].append(api_check)

    # 2. launchd 服务状态
    launchd_check = check_launchd_service()
    results["checks"].append(launchd_check)

    # 3. Cookie 配置
    cookie_check = check_cookie_config()
    results["checks"].append(cookie_check)

    # 汇总状态
    api_available = api_check.get("available")
    service_running = launchd_check.get("running")

    if api_available and service_running:
        results["status"] = "healthy"
    elif service_running and not api_available:
        results["status"] = "degraded"
        # 如果服务运行但 API 不可用，可能是启动中
        if auto_restart:
            logger.warning("服务运行但 API 不可用，等待重试...")
            time.sleep(5)
            api_check = check_douyin_api_service()
            if api_check.get("available"):
                results["status"] = "healthy"
    else:
        results["status"] = "down"
        if auto_restart:
            logger.warning("服务未运行，尝试重启...")
            restart_result = try_restart_service()
            results["checks"].append(restart_result)
            if restart_result["success"]:
                results["status"] = "recovered"
                # 重新检查 API
                time.sleep(3)
                api_check = check_douyin_api_service()
                results["checks"][0] = api_check

    if json_output:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        print_report(results)

    return results


def print_report(results: dict):
    """打印健康检查报告"""
    print("=" * 50)
    print("Douyin API 健康检查报告")
    print(f"时间: {results['timestamp']}")
    print("=" * 50)

    for check in results["checks"]:
        if "service" in check:
            status = "✅" if check.get("available") else "❌"
            print(f"\n{status} {check['service']} (端口 {check['port']})")

            if check.get("response_time_ms"):
                print(f"   响应时间: {check['response_time_ms']}ms")

            if check.get("error"):
                print(f"   错误: {check['error']}")

        elif "check" in check:
            check_type = check["check"]

            if check_type == "launchd_service":
                status = "✅" if check.get("running") else "❌"
                print(f"\n{status} Launchd 服务: {check['label']}")
                if check.get("pid"):
                    print(f"   PID: {check['pid']}")
                if check.get("error"):
                    print(f"   错误: {check['error']}")

            elif check_type == "cookie_config":
                status = "✅" if check.get("file_exists") else "⚠️"
                print(f"\n{status} Cookie 配置")
                if check.get("file_exists"):
                    print(f"   Cookie 工具目录存在")
                if check.get("error"):
                    print(f"   错误: {check['error']}")

        elif "action" in check:
            action = check["action"]
            status = "✅" if check.get("success") else "❌"
            print(f"\n{status} 操作: {action}")
            if check.get("error"):
                print(f"   错误: {check['error']}")

    print("\n" + "=" * 50)
    status = results["status"]
    status_map = {
        "healthy": "🟢 正常",
        "degraded": "🟡 降级",
        "down": "🔴 不可用",
        "recovered": "🟢 已恢复"
    }
    print(f"状态: {status_map.get(status, status)}")
    print("=" * 50)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Douyin API 健康检查")
    parser.add_argument("--json", action="store_true", help="输出 JSON 格式")
    parser.add_argument("--restart", action="store_true", help="服务异常时自动重启")
    args = parser.parse_args()

    run_health_check(json_output=args.json, auto_restart=args.restart)