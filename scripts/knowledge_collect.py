#!/usr/bin/env python3
"""
knowledge_collect.py - 网页收录到知识库
迁移自 OpenClaw knowledge-collect Skill，合并 websummary.py 抓取能力

功能：
1. 抓取网页内容
2. 提取正文
3. 保存到 Obsidian Inbox
4. 输出 JSON 供调用方解析
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from html.parser import HTMLParser
from typing import Tuple, Optional, Dict, Any

# 路径配置
DEFAULT_INBOX = Path("~/Documents/ZhiweiVault/Inbox").expanduser()


class TextExtractor(HTMLParser):
    """从 HTML 提取正文文本"""

    SKIP_TAGS = {
        "script", "style", "nav", "header", "footer", "aside",
        "noscript", "iframe", "svg", "form", "button"
    }

    def __init__(self):
        super().__init__()
        self.result = []
        self.title = ""
        self.skip_depth = 0
        self.in_title = False
        self.meta = {}

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
        if tag == "title":
            self.in_title = True
        if tag == "meta":
            attr_dict = dict(attrs)
            name = attr_dict.get("name", attr_dict.get("property", ""))
            content = attr_dict.get("content", "")
            if name and content:
                self.meta[name] = content

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS and self.skip_depth > 0:
            self.skip_depth -= 1
        if tag == "title":
            self.in_title = False
        if tag in ("p", "div", "article", "section", "br", "li", "h1",
                   "h2", "h3", "h4", "h5", "h6", "blockquote", "tr"):
            self.result.append("\n")

    def handle_data(self, data):
        if self.in_title:
            self.title += data.strip()
        if self.skip_depth == 0:
            text = data.strip()
            if text:
                self.result.append(text + " ")

    def get_text(self) -> str:
        raw = "".join(self.result)
        lines = raw.splitlines()
        cleaned = []
        for line in lines:
            line = line.strip()
            if line:
                cleaned.append(line)
            elif cleaned and cleaned[-1] != "":
                cleaned.append("")
        return "\n".join(cleaned)


def fetch_url(url: str, timeout: int = 20) -> Tuple[Optional[str], Optional[str]]:
    """用 curl 抓取网页"""
    try:
        result = subprocess.run(
            [
                "curl", "-4", "-sfL", "--max-time", str(timeout),
                "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "-H", "Accept: text/html,application/xhtml+xml",
                "-H", "Accept-Language: zh-CN,zh;q=0.9,en;q=0.8",
                url
            ],
            capture_output=True, text=True, timeout=timeout + 5
        )
        if result.returncode != 0:
            return None, f"curl 返回码: {result.returncode}"
        return result.stdout, None
    except subprocess.TimeoutExpired:
        return None, "请求超时"
    except Exception as e:
        return None, str(e)


def extract_content(html: str) -> Tuple[str, str, Dict[str, str]]:
    """从 HTML 提取标题、正文、元信息"""
    extractor = TextExtractor()
    try:
        extractor.feed(html)
    except Exception:
        pass
    return extractor.title, extractor.get_text(), extractor.meta


def sanitize_filename(title: str) -> str:
    """生成安全文件名"""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', title)
    name = name[:80].strip()
    if not name:
        name = "untitled"
    return name


def save_to_inbox(
    url: str,
    title: str,
    content: str,
    meta: Dict[str, str],
    tags: str = "",
    inbox_dir: Path = None,
    generate_ai_summary: bool = True
) -> str:
    """保存到 Obsidian Inbox，可选生成 AI 硬件架构师专属摘要"""
    inbox = inbox_dir or DEFAULT_INBOX
    inbox.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_title = sanitize_filename(title)
    filename = f"{safe_title}_{timestamp}.md"
    filepath = inbox / filename

    # 获取 meta description
    description = meta.get("description", meta.get("og:description", ""))

    # 构建 Obsidian 笔记
    md_content = f"""---
title: "{title}"
url: "{url}"
collected_at: "{datetime.now().isoformat()}"
tags: [{tags}]
status: unprocessed
---

# {title}

> 来源: [{url}]({url})
> 收录时间: {datetime.now().strftime("%Y-%m-%d %H:%M")}
"""

    # Meta 摘要
    if description:
        md_content += f"\n## 摘要\n\n{description}\n"

    # 生成 AI 硬件架构师专属摘要
    if generate_ai_summary and content:
        try:
            from obsidian_summary_filler import generate_ai_summary_for_obsidian
            ai_summary = generate_ai_summary_for_obsidian(content, title, doc_type="网页")
            if ai_summary:
                md_content += ai_summary
        except Exception as e:
            print(f"⚠️ AI 摘要生成失败: {e}")

    md_content += f"\n## 正文\n\n{content}\n"

    filepath.write_text(md_content, encoding="utf-8")
    return str(filepath)


def collect_url(
    url: str,
    tags: str = "",
    inbox_dir: Path = None
) -> Dict[str, Any]:
    """收录网页主函数"""

    # Step 1: 抓取
    html, error = fetch_url(url)
    if error:
        return {"status": "error", "message": error}

    # Step 2: 提取
    title, content, meta = extract_content(html)
    if not content.strip():
        return {"status": "error", "message": "未能提取到正文内容（可能是 JS 渲染页面）"}

    # Step 3: 保存
    filepath = save_to_inbox(
        url=url,
        title=title or "无标题",
        content=content,
        meta=meta,
        tags=tags,
        inbox_dir=inbox_dir
    )

    return {
        "status": "ok",
        "title": title or "无标题",
        "file": filepath,
        "word_count": len(content),
        "url": url
    }


def main():
    parser = argparse.ArgumentParser(description="收录网页到知识库")
    parser.add_argument("--url", required=True, help="要收录的 URL")
    parser.add_argument("--tags", default="", help="标签，逗号分隔")
    parser.add_argument("--inbox", default=None, help="自定义 Inbox 目录")

    args = parser.parse_args()

    inbox_dir = Path(args.inbox) if args.inbox else None

    result = collect_url(args.url, args.tags, inbox_dir)
    print(json.dumps(result, ensure_ascii=False))

    if result["status"] != "ok":
        sys.exit(1)


if __name__ == "__main__":
    main()