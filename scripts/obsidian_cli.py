#!/usr/bin/env python3
"""
Obsidian CLI - 知微 Vault 管理工具

功能:
- 搜索笔记 (全文搜索 + 标签搜索)
- 读取笔记内容
- 创建/更新笔记
- 列出最近笔记
- 统计信息

用法:
    obsidian-cli search <query>
    obsidian-cli read <note_name>
    obsidian-cli create <title> [--category Inbox] [--tags tag1,tag2]
    obsidian-cli recent [--limit 10]
    obsidian-cli stats
"""
import sys
import os
import re
import json
import argparse
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict

# Vault 路径
VAULT_PATH = Path.home() / "Documents" / "ZhiweiVault"

# 分类目录映射
CATEGORIES = {
    "AI-Systems": "10-19_AI系统_AI-Systems",
    "AI-Hardware": "20-29_AI硬件_AI-Hardware",
    "Infra": "30-39_基础设施_Infra-Compute",
    "Networking": "40-49_网络与互联_Networking",
    "AI-Briefs": "41_AI简报_AI-Briefs",
    "Industry": "50-59_行业研究_Industry",
    "Business": "60-69_商业与管理_Business",
    "Personal": "70-79_个人笔记_Personal",
    "Work": "80-89_工作文档_Work",
    "System": "90-99_系统与归档_System",
    "Inbox": "Inbox"
}


def search_notes(query: str, limit: int = 20, json_output: bool = False) -> List[Dict]:
    """搜索笔记内容"""
    results = []
    query_lower = query.lower()

    # 使用 ripgrep 或 grep
    try:
        import subprocess
        # 尝试用 rg (更快)
        result = subprocess.run(
            ["rg", "-l", "-i", query, str(VAULT_PATH), "--type", "md"],
            capture_output=True, text=True, timeout=30
        )
        files = result.stdout.strip().split('\n') if result.stdout.strip() else []
    except (FileNotFoundError, subprocess.TimeoutExpired):
        # 降级到 grep
        result = subprocess.run(
            ["grep", "-r", "-l", "-i", query, str(VAULT_PATH), "--include=*.md"],
            capture_output=True, text=True, timeout=60
        )
        files = result.stdout.strip().split('\n') if result.stdout.strip() else []

    for filepath in files[:limit]:
        if not filepath:
            continue
        path = Path(filepath)
        try:
            # 读取前几行获取标题
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read(500)  # 只读前 500 字符
                title = extract_title(content, path.stem)

            results.append({
                "title": title,
                "path": str(path.relative_to(VAULT_PATH)),
                "wikilink": f"[[{title}]]"
            })
        except Exception:
            pass

    return results


def read_note(note_name: str) -> Optional[Dict]:
    """读取笔记内容"""
    # 尝试直接匹配
    for md_file in VAULT_PATH.rglob("*.md"):
        if note_name.lower() in md_file.stem.lower():
            try:
                with open(md_file, 'r', encoding='utf-8') as f:
                    content = f.read()
                return {
                    "title": extract_title(content, md_file.stem),
                    "path": str(md_file.relative_to(VAULT_PATH)),
                    "content": content,
                    "size": len(content),
                    "modified": datetime.fromtimestamp(md_file.stat().st_mtime).isoformat()
                }
            except Exception:
                pass
    return None


def create_note(title: str, content: str = "", category: str = "Inbox",
                tags: List[str] = None, status: str = "draft") -> Dict:
    """创建笔记"""
    # 确定目录
    dir_name = CATEGORIES.get(category, category)
    target_dir = VAULT_PATH / dir_name
    target_dir.mkdir(parents=True, exist_ok=True)

    # 生成文件名
    date_str = datetime.now().strftime("%Y-%m-%d")
    safe_title = "".join(c if c.isalnum() or c in " -_" else "_" for c in title)
    filename = f"NOTE_{date_str}_{safe_title[:50]}.md"
    filepath = target_dir / filename

    # 构建 frontmatter
    frontmatter = [
        "---",
        f'title: "{title}"',
        f"date: {datetime.now().isoformat()}",
    ]
    if tags:
        frontmatter.append("tags:")
        for tag in tags:
            frontmatter.append(f"  - {tag}")
    frontmatter.append(f"status: {status}")
    frontmatter.append("---")
    frontmatter.append("")

    full_content = "\n".join(frontmatter) + content

    # 写入文件
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(full_content)

    return {
        "success": True,
        "path": str(filepath.relative_to(VAULT_PATH)),
        "wikilink": f"[[{title}]]",
        "size": len(full_content)
    }


def list_recent(limit: int = 10) -> List[Dict]:
    """列出最近的笔记"""
    notes = []

    for md_file in VAULT_PATH.rglob("*.md"):
        try:
            mtime = md_file.stat().st_mtime
            notes.append((mtime, md_file))
        except Exception:
            pass

    # 按修改时间排序
    notes.sort(reverse=True)

    results = []
    for mtime, filepath in notes[:limit]:
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read(500)
            title = extract_title(content, filepath.stem)
            results.append({
                "title": title,
                "path": str(filepath.relative_to(VAULT_PATH)),
                "modified": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M"),
                "wikilink": f"[[{title}]]"
            })
        except Exception:
            pass

    return results


def show_stats() -> Dict:
    """显示统计信息"""
    total_files = 0
    total_size = 0
    by_category = {}

    for md_file in VAULT_PATH.rglob("*.md"):
        total_files += 1
        total_size += md_file.stat().st_size

        # 按一级目录分类
        rel_path = md_file.relative_to(VAULT_PATH)
        category = rel_path.parts[0] if rel_path.parts else "Unknown"
        by_category[category] = by_category.get(category, 0) + 1

    return {
        "vault_path": str(VAULT_PATH),
        "total_notes": total_files,
        "total_size_mb": round(total_size / 1024 / 1024, 2),
        "by_category": dict(sorted(by_category.items(), key=lambda x: -x[1]))
    }


def extract_title(content: str, fallback: str) -> str:
    """从内容中提取标题"""
    # 尝试从 frontmatter 提取
    if content.startswith("---"):
        match = re.search(r'^title:\s*["\']?(.+?)["\']?\s*$', content, re.MULTILINE)
        if match:
            return match.group(1).strip('"\'')

    # 尝试从第一个 # 标题提取
    match = re.search(r'^#\s+(.+)$', content, re.MULTILINE)
    if match:
        return match.group(1).strip()

    return fallback


def main():
    parser = argparse.ArgumentParser(
        description="Obsidian CLI - 知微 Vault 管理工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  obsidian-cli search "AI Agent"
  obsidian-cli read "深度情报"
  obsidian-cli create "新笔记标题" --category Personal --tags AI,note
  obsidian-cli recent --limit 20
  obsidian-cli stats
        """
    )

    subparsers = parser.add_subparsers(dest="command", help="命令")

    # search
    search_parser = subparsers.add_parser("search", help="搜索笔记")
    search_parser.add_argument("query", help="搜索关键词")
    search_parser.add_argument("--limit", "-l", type=int, default=20, help="结果数量")
    search_parser.add_argument("--json", "-j", action="store_true", help="JSON 输出")

    # read
    read_parser = subparsers.add_parser("read", help="读取笔记")
    read_parser.add_argument("note", help="笔记名称")

    # create
    create_parser = subparsers.add_parser("create", help="创建笔记")
    create_parser.add_argument("title", help="笔记标题")
    create_parser.add_argument("--category", "-c", default="Inbox", help="分类目录")
    create_parser.add_argument("--tags", "-t", default="", help="标签 (逗号分隔)")
    create_parser.add_argument("--content", help="笔记内容")
    create_parser.add_argument("--status", default="draft", help="状态")

    # recent
    recent_parser = subparsers.add_parser("recent", help="最近笔记")
    recent_parser.add_argument("--limit", "-l", type=int, default=10, help="数量")

    # stats
    subparsers.add_parser("stats", help="统计信息")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    if args.command == "search":
        results = search_notes(args.query, args.limit, args.json)
        if args.json:
            print(json.dumps(results, ensure_ascii=False, indent=2))
        else:
            if not results:
                print("未找到匹配的笔记")
            else:
                print(f"找到 {len(results)} 条结果:\n")
                for r in results:
                    print(f"  [[{r['title']}]]")
                    print(f"    {r['path']}\n")

    elif args.command == "read":
        result = read_note(args.note)
        if result:
            print(f"标题: {result['title']}")
            print(f"路径: {result['path']}")
            print(f"大小: {result['size']} 字节")
            print(f"修改: {result['modified']}")
            print("\n" + "="*50 + "\n")
            print(result['content'])
        else:
            print(f"未找到笔记: {args.note}")

    elif args.command == "create":
        tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else []
        content = args.content or ""
        result = create_note(args.title, content, args.category, tags, args.status)
        print(f"✅ 笔记已创建: {result['path']}")
        print(f"   Wikilink: {result['wikilink']}")

    elif args.command == "recent":
        results = list_recent(args.limit)
        print(f"最近 {len(results)} 条笔记:\n")
        for r in results:
            print(f"  [{r['modified']}] [[{r['title']}]]")

    elif args.command == "stats":
        stats = show_stats()
        print(f"Vault 路径: {stats['vault_path']}")
        print(f"笔记总数: {stats['total_notes']}")
        print(f"总大小: {stats['total_size_mb']} MB")
        print("\n按目录分布:")
        for cat, count in list(stats['by_category'].items())[:10]:
            print(f"  {cat}: {count}")


if __name__ == "__main__":
    main()