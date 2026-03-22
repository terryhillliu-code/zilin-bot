#!/usr/bin/env python3
"""
知微系统健康检查工具类
"""

import subprocess
import json
import os
from pathlib import Path

def get_system_health_dict() -> dict:
    """获取系统健康状态字典"""
    status = {
        "services": {},
        "docker": {}
    }

    # 1. 检查 launchd 服务
    services = [
        "com.zhiwei.bot",
        "com.zhiwei.scheduler",
        "com.zhiwei.dev-worker",
        "com.zhiwei.rag-api"
    ]

    try:
        result = subprocess.run(
            ["launchctl", "list"],
            capture_output=True,
            text=True,
            timeout=5
        )

        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                parts = line.split()
                if len(parts) >= 3:
                    pid, last_exit_code, name = parts[0], parts[1], parts[2]
                    if name in services:
                        is_running = pid != "-"
                        
                        if name == "com.zhiwei.rag-api" and is_running:
                            # 额外检查 RAG 接口
                            try:
                                import requests
                                resp = requests.get("http://127.0.0.1:8765/health", timeout=3)
                                if resp.ok:
                                    health_data = resp.json()
                                    status["services"][name] = {
                                        "status": "healthy",
                                        "pid": pid,
                                        "embedding_loaded": health_data.get("embedding_loaded"),
                                        "reranker_loaded": health_data.get("reranker_loaded")
                                    }
                                else:
                                    status["services"][name] = {"status": "degraded", "pid": pid}
                            except:
                                status["services"][name] = {"status": "running", "pid": pid}
                        else:
                            status["services"][name] = {
                                "status": "running" if is_running else "stopped",
                                "pid": pid
                            }
    except Exception as e:
        status["services"]["error"] = str(e)

    # 2. 检查 Docker (优化版：增加环境兼容性处理)
    try:
        # 使用更稳健的命令，硬熔断：超时设为 2s，避免假死卡顿
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"],
            capture_output=True,
            text=True,
            timeout=2,
            env={**os.environ, "DOCKER_API_VERSION": "1.41"} # 尝试锁定较低的 API 版本以提高兼容性
        )

        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                if line:
                    parts = line.split("\t")
                    if len(parts) >= 2:
                        status["docker"][parts[0]] = parts[1]
        else:
            # 降级：仅列出容器名，极速模式：1s 超时
            result = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}"], capture_output=True, text=True, timeout=1)
            if result.returncode == 0:
                for name in result.stdout.strip().split("\n"):
                    if name: status["docker"][name] = "unknown (API Error)"
    except Exception as e:
        status["docker"]["error"] = f"Timeout or Error (>2s 断路保护): {str(e)}"

    return status

def format_health_status(status: dict) -> str:
    """将健康状态字典格式化为 Markdown 文本"""
    lines = ["📊 **知微系统状态**\n"]
    
    # 服务
    lines.append("**基础服务:**")
    for name, info in status.get("services", {}).items():
        if name == "error": continue
        s = info.get("status", "unknown")
        emoji = "✅" if s in ["healthy", "running"] else "⚠️" if s == "degraded" else "❌"
        lines.append(f"  • {emoji} {name.replace('com.zhiwei.', '')}: {s}")
    
    # Docker
    docker = status.get("docker", {})
    if docker and "error" not in docker:
        lines.append("\n**容器状态:**")
        for name, s in docker.items():
            emoji = "✅" if "Up" in s else "❌"
            lines.append(f"  • {emoji} {name}: {s}")
    elif "error" in docker:
        lines.append(f"\n⚠️ **Docker 检查失败**: {docker['error']}")
        
    return "\n".join(lines)

if __name__ == "__main__":
    status = get_system_health_dict()
    print(format_health_status(status))
