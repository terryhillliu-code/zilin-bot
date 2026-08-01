#!/usr/bin/env python3
"""URL 投喂 CLI（S2, 2026-07-31）

背景：粘贴视频/文章链接是入站消息的 88%（message_log 全量统计），此前只能
在飞书里做。Web 控制台需要同一能力，但**不能重复实现**——cookies 平台策略、
video_history 记账、distiller 调用、摘要抽取都只在 media_handler 里有一份。

本脚本是薄包装：解析参数 → 调 media_handler.process_video() → 输出结果。
控制台以 subprocess 调用（需 zhiwei-shared-venv 解释器），从而与飞书路径
共用同一套实现，避免"同类功能不同实现"。

用法:
    python ingest_url.py --url "<链接或含链接的文本>" [--json]
"""
import argparse
import json
import os
import sys
import warnings

warnings.simplefilter("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    ap = argparse.ArgumentParser(description="投喂一条 URL（视频/文章）")
    ap.add_argument("--url", required=True, help="链接，或含链接的分享文本")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = ap.parse_args()

    import media_handler

    try:
        result = media_handler.process_video(args.url)
        ok = result.startswith("✅")
    except Exception as e:
        result = f"❌ 处理异常: {e}"
        ok = False

    if args.json:
        print(json.dumps({"ok": ok, "result": result}, ensure_ascii=False))
    else:
        print(result)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
