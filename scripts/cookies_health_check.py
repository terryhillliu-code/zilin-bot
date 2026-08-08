#!/usr/bin/env python3
"""媒体链路 cookies/隧道健康检查 (v71.3, 2026-07-31)

检查项:
1. jp-tunnel SOCKS5 隧道可用性(YouTube 可达)
2. VM POT 服务(bgutil, 经隧道 14416)
3. YouTube cookies 功能探测(yt-dlp 实际取标题, 最可靠)
4. YouTube 关键 cookie 剩余寿命(__Secure-1PSIDTS ~2周轮换, 提前预警)
5. 抖音 cookies 文件年龄(功能检测由 douyin_health_check 独立覆盖)

输出 JSON: {"status": "healthy|degraded|unhealthy", "checks": {...}, "advice": [...]}
被 scheduler 的 cookies_health_check job 以 subprocess 调用(shared venv)。
"""
import json
import os  # 2026-08-09: 补齐——try_self_heal_youtube 用到, 缺失导致自愈从未生效
import subprocess
import sys
import time
from pathlib import Path

PROXY = "socks5://127.0.0.1:18081"
PROXY_H = "socks5h://127.0.0.1:18081"
POT_URL = "http://127.0.0.1:14416/ping"
YT_COOKIES = Path.home() / "zhiwei-bot" / "secrets" / "youtube_cookies.txt"
DY_COOKIES = Path.home() / "zhiwei-bot" / "secrets" / "douyin_cookies.txt"
YTDLP = str(Path.home() / "zhiwei-shared-venv" / "bin" / "yt-dlp")
# 探测视频(2026-08-02 更换): 原月之暗面视频对匿名/失效 cookies 也放行,
# 无法识别 bot 检测(凌晨 YouTube 追更全灭但健康检查误报 healthy)。
# 换成实测需要有效登录态才能解锁的视频, 确保探测严格。
PROBE_URL = "https://www.youtube.com/watch?v=LIPzl4OnlTo"

# ⭐ 2026-08-09: 加 --proxy——本机直连 youtube.com 不通, 此前自愈命令
# 必超时失败(叠加缺 import os, 自愈从未真正生效过)。socks5h = DNS 也走代理。
_YT_REFRESH_PROXY = os.getenv("ZHIWEI_VIDEO_PROXY", "socks5://127.0.0.1:18081")
_YT_REFRESH_PROXY = _YT_REFRESH_PROXY.replace("socks5://", "socks5h://", 1)
# ⭐ 2026-08-09: 登录态在 Profile 1(Default 从未登录), 必须指定配置文件,
# 否则永远导出空会话(自愈第三处 bug)
_YT_CHROME_PROFILE = os.getenv("ZHIWEI_YT_CHROME_PROFILE", "Profile 1")
REFRESH_CMD = (f"~/zhiwei-shared-venv/bin/yt-dlp --proxy {_YT_REFRESH_PROXY} "
               f'--cookies-from-browser "chrome:{_YT_CHROME_PROFILE}" '
               "--cookies ~/zhiwei-bot/secrets/youtube_cookies.txt --skip-download "
               '"https://www.youtube.com/watch?v=aqz-KE-bpKQ"')


def _webkit_or_unix_to_epoch(ts: float) -> float:
    """兼容 Chrome WebKit 微秒(自1601)与 Unix 秒两种时间戳"""
    if ts > 1e16:  # WebKit microseconds
        return ts / 1e6 - 11644473600
    if ts > 1e12:  # 毫秒
        return ts / 1e3
    return ts


def check_tunnel() -> dict:
    """经隧道探测 YouTube 可达性"""
    try:
        import requests
        r = requests.get("https://www.youtube.com/robots.txt", timeout=15,
                         proxies={"http": PROXY_H, "https": PROXY_H})
        return {"ok": r.status_code == 200, "detail": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"ok": False, "detail": str(e)[:120]}


def check_pot() -> dict:
    """POT 服务 ping(经隧道转发到 VM)"""
    try:
        import requests
        r = requests.get(POT_URL, timeout=10)
        data = r.json()
        return {"ok": True, "detail": f"v{data.get('version')} uptime {int(data.get('server_uptime', 0))}s"}
    except Exception as e:
        return {"ok": False, "detail": str(e)[:120]}


def check_youtube_cookies_functional() -> dict:
    """功能探测: cookies + 隧道真实取一次视频标题(不下载)"""
    if not YT_COOKIES.exists():
        return {"ok": False, "detail": "youtube_cookies.txt 不存在"}
    try:
        # 2026-08-02: 探测命令补齐 POT/EJS 三件套(与生产调用一致)——
        # 缺三件套时探测结果不代表真实解锁能力(YouTube 概率风控下偏乐观)
        result = subprocess.run(
            [YTDLP, "--proxy", PROXY, "--cookies", str(YT_COOKIES),
             "--extractor-args", f"youtubepot-bgutilhttp:base_url={POT_URL.replace('/ping', '')}",
             "--remote-components", "ejs:github",
             "--ignore-no-formats-error", "--skip-download", "--no-warnings",
             "--print", "title", PROBE_URL],
            capture_output=True, text=True, timeout=120)
        title = (result.stdout or "").strip().splitlines()
        if result.returncode == 0 and title:
            return {"ok": True, "detail": f"取标题成功: {title[0][:30]}"}
        err = (result.stderr or "").strip()[-200:]
        # bot 检测特征 = cookies 失效
        expired = "Sign in to confirm" in err
        return {"ok": False, "expired": expired, "detail": err[:150]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "detail": "探测超时(120s), 疑似隧道阻塞"}
    except Exception as e:
        return {"ok": False, "detail": str(e)[:120]}


def check_youtube_cookies_lifetime() -> dict:
    """解析关键短命 cookie 剩余天数(__Secure-1PSIDTS 约 2 周轮换)"""
    if not YT_COOKIES.exists():
        return {"ok": False, "detail": "文件不存在"}
    now = time.time()
    min_days = None
    watch = ("__Secure-1PSIDTS", "__Secure-3PSIDTS", "LOGIN_INFO")
    try:
        for line in YT_COOKIES.read_text(errors="ignore").splitlines():
            if line.startswith("#") or not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 7 and parts[5] in watch:
                try:
                    exp = _webkit_or_unix_to_epoch(float(parts[4]))
                except ValueError:
                    continue
                days = (exp - now) / 86400
                if min_days is None or days < min_days:
                    min_days = days
        if min_days is None:
            return {"ok": True, "detail": "未找到轮换型 cookie(可忽略)"}
        return {"ok": min_days > 3, "days_left": round(min_days, 1),
                "detail": f"最短寿命 cookie 剩 {min_days:.1f} 天"}
    except OSError as e:
        return {"ok": False, "detail": str(e)[:120]}


def check_douyin_cookies_age() -> dict:
    """抖音 cookies 文件年龄(>30 天提示关注; 功能有效性由 douyin_health_check 覆盖)"""
    if not DY_COOKIES.exists():
        return {"ok": False, "detail": "douyin_cookies.txt 不存在"}
    age_days = (time.time() - DY_COOKIES.stat().st_mtime) / 86400
    return {"ok": age_days < 30, "age_days": round(age_days, 1),
            "detail": f"文件年龄 {age_days:.0f} 天"}


def try_self_heal_youtube() -> dict:
    """YouTube cookies 自愈: 执行 REFRESH_CMD(Chrome 新鲜登录态 +
    滚动 cookies 写回文件) 后复测。2026-08-02 新增。
    """
    import shlex
    cmd_str = os.path.expandvars(REFRESH_CMD.replace("~", str(Path.home())))
    try:
        r = subprocess.run(shlex.split(cmd_str), capture_output=True, text=True, timeout=180)
        time.sleep(2)
        recheck = check_youtube_cookies_functional()
        return {"attempted": True, "refresh_rc": r.returncode, "recheck": recheck}
    except Exception as e:
        return {"attempted": True, "refresh_rc": -1,
                "recheck": {"ok": False, "detail": f"自愈异常: {str(e)[:100]}"}}


def main():
    checks = {
        "tunnel": check_tunnel(),
        "pot_server": check_pot(),
        "youtube_cookies": check_youtube_cookies_functional(),
        "youtube_cookie_lifetime": check_youtube_cookies_lifetime(),
        "douyin_cookies_age": check_douyin_cookies_age(),
    }

    # 2026-08-02 自愈: 功能探测命中 bot 检测(expired)时, 先自动刷新再复测,
    # 恢复则免告警; 仅 Chrome 登录态也失效时才升级为用户告警。
    if not checks["youtube_cookies"]["ok"] and checks["youtube_cookies"].get("expired"):
        heal = try_self_heal_youtube()
        checks["youtube_self_heal"] = heal
        if heal["recheck"].get("ok"):
            checks["youtube_cookies"] = {
                "ok": True,
                "detail": f"自愈成功(Chrome 滚动刷新): {heal['recheck'].get('detail', '')[:60]}",
            }
            checks["youtube_cookie_lifetime"] = check_youtube_cookies_lifetime()

    advice = []
    if not checks["tunnel"]["ok"]:
        advice.append("隧道异常: launchctl kickstart -k gui/$(id -u)/com.zhiwei.jp-tunnel")
    if not checks["pot_server"]["ok"]:
        advice.append("POT 服务异常: ssh root@47.79.87.32 'systemctl restart bgutil-pot'")
    if not checks["youtube_cookies"]["ok"] or not checks["youtube_cookie_lifetime"]["ok"]:
        advice.append(f"刷新 YouTube cookies(需 Chrome 已登录): {REFRESH_CMD}")
    if not checks["douyin_cookies_age"]["ok"]:
        advice.append("抖音 cookies 偏旧, 若 douyin_health_check 报错请从浏览器重新导出")

    # 状态分级: 功能探测/隧道挂 = unhealthy; 仅寿命预警/文件偏旧 = degraded
    critical_fail = not (checks["tunnel"]["ok"] and checks["pot_server"]["ok"]
                         and checks["youtube_cookies"]["ok"])
    warn = not (checks["youtube_cookie_lifetime"]["ok"] and checks["douyin_cookies_age"]["ok"])
    status = "unhealthy" if critical_fail else ("degraded" if warn else "healthy")

    print(json.dumps({"status": status, "checks": checks, "advice": advice},
                     ensure_ascii=False, indent=1))
    return 0 if status == "healthy" else 1


if __name__ == "__main__":
    sys.exit(main())
