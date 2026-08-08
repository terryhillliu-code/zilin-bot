#!/usr/bin/env python3
"""YouTube cookies 导入工具（2026-08-09）

背景: 本机 Chrome 的 YouTube 会话已失效且无法从浏览器自动导出有效登录态
(自愈 cookies-from-browser 拿不到用户真实登录的浏览器)。恢复路径改为:
用户在常用浏览器用 Cookie 扩展导出 JSON → 本脚本转 Netscape 格式写入
secrets/youtube_cookies.txt。

用法:
    python3 yt_cookies_import.py <导出文件.json>
    cat export.json | python3 yt_cookies_import.py -
"""
import json
import sys
from pathlib import Path

TARGET = Path.home() / "zhiwei-bot" / "secrets" / "youtube_cookies.txt"


def main() -> int:
    src = sys.argv[1] if len(sys.argv) > 1 else "-"
    raw = sys.stdin.read() if src == "-" else Path(src).read_text(encoding="utf-8")
    cookies = json.loads(raw)
    if not isinstance(cookies, list):
        print("❌ 期望 JSON 数组(Cookie 扩展导出格式)"); return 1

    lines = ["# Netscape HTTP Cookie File",
             f"# youtube.com cookies - 浏览器扩展导出导入"]
    for c in cookies:
        d = c.get("domain", "")
        flag = "TRUE" if d.startswith(".") else "FALSE"
        secure = "TRUE" if c.get("secure") else "FALSE"
        exp = int(c.get("expirationDate", 0))
        lines.append(f"{d}\t{flag}\t{c.get('path','/')}\t{secure}\t{exp}\t"
                     f"{c['name']}\t{c['value']}")

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    bak = TARGET.with_suffix(".txt.bak")
    if TARGET.exists():
        bak.write_bytes(TARGET.read_bytes())
    TARGET.write_text("\n".join(lines) + "\n", encoding="utf-8")
    n_login = sum(1 for c in cookies if c["name"] == "LOGIN_INFO")
    print(f"✓ 写入 {len(cookies)} 条 cookies → {TARGET}")
    print(f"  LOGIN_INFO: {n_login} {'✅' if n_login else '⚠️ 无登录态cookie, 可能仍会失败'}")
    if bak.exists():
        print(f"  旧文件备份: {bak}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
