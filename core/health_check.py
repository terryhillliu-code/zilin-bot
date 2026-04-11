#!/usr/bin/env python3
"""
知微系统健康检查工具类 (v1.3.0 - 缓存增强版)
"""

import subprocess
import json
import os
from pathlib import Path
from datetime import datetime
import shutil

# Docker 路径 (针对 Cron/Launchd 环境)
DOCKER_BIN = "/usr/local/bin/docker"
if not Path(DOCKER_BIN).exists():
    DOCKER_BIN = shutil.which("docker") or "docker"

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
                            except Exception:
                                status["services"][name] = {"status": "running", "pid": pid}
                        else:
                            status["services"][name] = {
                                "status": "running" if is_running else "stopped",
                                "pid": pid
                            }
    except Exception as e:
        status["services"]["error"] = str(e)

    # 2. 检查 Docker (优先使用缓存以规避 P0 级假死风险)
    cache_path = Path.home() / ".cache" / "docker_status.json"
    used_cache = False
    
    if cache_path.exists():
        try:
            with open(cache_path, "r") as f:
                cache_data = json.load(f)
            
            # 校验缓存时效性 (120秒内视为有效)
            updated_at_str = cache_data.get("updated_at", "")
            if updated_at_str:
                updated_at = datetime.fromisoformat(updated_at_str)
                # 处理可能带时区的情况
                now = datetime.now()
                if updated_at.tzinfo:
                    from datetime import timezone
                    now = datetime.now(timezone.utc)
                
                if (now - updated_at).total_seconds() < 120:
                    for name, info in cache_data.get("containers", {}).items():
                        # 转换格式以匹配原有输出：Up 10m (healthy)
                        status_str = f"{info.get('status', 'unknown')}"
                        if info.get('health') and info.get('health') != "N/A":
                            status_str += f" ({info.get('health')})"
                        if info.get('uptime') and info.get('uptime') != "N/A":
                            status_str = f"Up {info.get('uptime')} / {status_str}"
                        
                        status["docker"][name] = status_str
                    status["docker_checked_at"] = updated_at_str
                    used_cache = True
        except Exception:
            pass

    if not used_cache:
        try:
            # 只有在缓存失效时才尝试直接查询，且严格限制 2s 超时
            result = subprocess.run(
                [DOCKER_BIN, "ps", "--format", "{{.Names}}\t{{.Status}}"],
                capture_output=True, text=True, timeout=2,
                env={**os.environ, "DOCKER_API_VERSION": "1.41"}
            )
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    if line:
                        parts = line.split("\t")
                        if len(parts) >= 2: status["docker"][parts[0]] = parts[1]
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
        checked_at = status.get("docker_checked_at", "实时")
        if "T" in checked_at:
            checked_at = checked_at.split("T")[1][:5] # 提取时间部分 HH:MM
        
        lines.append(f"\n**容器状态 ({checked_at}):**")
        for name, s in docker.items():
            emoji = "✅" if "Up" in s or "running" in s else "❌"
            lines.append(f"  • {emoji} {name}: {s}")
    elif "error" in docker:
        lines.append(f"\n⚠️ **Docker 检查失败**: {docker['error']}")
        
    return "\n".join(lines)

if __name__ == "__main__":
    status = get_system_health_dict()
    print(format_health_status(status))
