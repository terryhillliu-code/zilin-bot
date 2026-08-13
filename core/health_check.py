#!/usr/bin/env python3
"""
知微系统健康检查工具类 (v1.4.0 - Docker 检查退役)

Docker 检查于 2026-08-09 随 Docker.app 整体退役（AGENTS.md 登记），
本模块不再探测容器状态，状态播报不再含容器段。
"""

import subprocess

def get_system_health_dict() -> dict:
    """获取系统健康状态字典"""
    status = {
        "services": {},
        # Docker 2026-08-09 整体退役（AGENTS.md 登记），状态播报不再含容器段
        "docker": {}
    }

    # 1. 检查 launchd 服务
    services = [
        "com.zhiwei.bot",
        "com.zhiwei.scheduler",
        # com.zhiwei.dev-worker 2026-07-25 整体下线（plist .DISABLED），目录保留（messages.db 为 MessageBus 活库）
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

    return "\n".join(lines)

if __name__ == "__main__":
    status = get_system_health_dict()
    print(format_health_status(status))
