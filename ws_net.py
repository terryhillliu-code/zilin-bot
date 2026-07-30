"""网络 / DNS 容错机制（2026-07-30 从 ws_client.py 拆分）"""

import socket
import struct
import time

# ========== DNS 容错机制 (2026-06-02 加固) ==========

def dns_resolve_with_retry(host: str, max_retries: int = 5, timeout: int = 3) -> str | None:
    """DNS 解析容错：指数退避重试，IPv4 优先。

    解决 macOS 在网络切换/VPN 环境下 DNS 解析不稳定导致的
    WebSocket 无法连接问题。
    """
    for attempt in range(max_retries):
        try:
            # AF_INET = IPv4 优先，避免 IPv6 解析超时
            result = socket.getaddrinfo(host, 443, socket.AF_INET)
            ip = result[0][4][0]
            if attempt > 0:
                print(f"✅ DNS 解析成功：{host} -> {ip} (重试 {attempt+1} 次)")
            else:
                print(f"✅ DNS 解析成功：{host} -> {ip}")
            return ip
        except socket.gaierror as e:
            delay = min(2 ** attempt, 16)  # 1s, 2s, 4s, 8s, 16s
            print(f"⚠️ DNS 解析失败 ({attempt+1}/{max_retries})：{host} - {e}，{delay}s 后重试")
            time.sleep(delay)
    print(f"❌ DNS 解析最终失败：{host}，将在 WebSocket 重连时继续尝试")
    return None


def check_dns_available(host: str = "open.feishu.cn") -> bool:
    """快速检查 DNS 是否可用，不阻塞"""
    try:
        socket.getaddrinfo(host, 443, socket.AF_INET)
        return True
    except socket.gaierror:
        return False
