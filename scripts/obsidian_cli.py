#!/usr/bin/env python3
"""
Obsidian CLI v2.0 - 知微 Vault 智能管理工具

新增功能:
- summarize: LLM 智能总结
- related: 知识关联分析
- share: 分享到飞书

改进功能:
- search: 多级优先级搜索 + 摘要预览
- read: 支持 --summarize 选项

用法:
    obsidian-cli search <query> [--preview] [--tag TAG]
    obsidian-cli read <note_name> [--summarize]
    obsidian-cli summarize <note_name>
    obsidian-cli related <note_name>
    obsidian-cli share <note_name> --to feishu [--mode summary|full]
    obsidian-cli create <title> [--category Inbox] [--tags tag1,tag2]
    obsidian-cli recent [--limit 10]
    obsidian-cli stats
"""
import sys
import os
import re
import json
import argparse
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Set

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

# 技术关键词同义词映射
TECH_SYNONYMS = {
    "ai": ["artificial intelligence", "人工智能"],
    "ml": ["machine learning", "机器学习"],
    "llm": ["large language model", "大语言模型", "大模型"],
    "rag": ["retrieval augmented generation", "检索增强生成"],
    "agent": ["智能体", "代理"],
    "transformer": ["注意力机制", "attention"],
}


# ============== 基础函数 ==============

def extract_title(content: str, fallback: str) -> str:
    """从内容中提取标题"""
    if content.startswith("---"):
        match = re.search(r'^title:\s*["\']?(.+?)["\']?\s*$', content, re.MULTILINE)
        if match:
            return match.group(1).strip('"\'')
    match = re.search(r'^#\s+(.+)$', content, re.MULTILINE)
    if match:
        return match.group(1).strip()
    return fallback


def extract_tags(content: str) -> List[str]:
    """从 frontmatter 提取标签"""
    tags = []
    if content.startswith("---"):
        # 提取 tags 块
        match = re.search(r'^tags:\s*\n((?:\s+-\s+.+\n?)+)', content, re.MULTILINE)
        if match:
            tag_block = match.group(1)
            tags = re.findall(r'-\s+(.+)', tag_block)
    # 也提取 #tag 格式
    tags.extend(re.findall(r'#(\w+)', content))
    return list(set(t.strip() for t in tags if t.strip()))


def extract_wikilinks(content: str) -> List[str]:
    """提取 [[wikilinks]]"""
    return list(set(re.findall(r'\[\[([^\]|]+)(?:\|[^\]]+)?\]\]', content)))


def extract_preview(content: str, query: str = None, max_len: int = 200) -> str:
    """提取摘要预览"""
    # 移除 frontmatter
    if content.startswith("---"):
        content = re.sub(r'^---\n.*?\n---\n', '', content, flags=re.DOTALL)

    # 移除标题
    content = re.sub(r'^#\s+.+$', '', content, flags=re.MULTILINE)

    # 移除 wikilinks 格式
    content = re.sub(r'\[\[([^\]|]+)(?:\|[^\]]+)?\]\]', r'\1', content)

    # 清理多余空白
    content = re.sub(r'\n+', ' ', content).strip()

    # 如果有查询词，优先显示包含查询词的句子
    if query:
        sentences = re.split(r'[。！？.!?]', content)
        for sent in sentences:
            if query.lower() in sent.lower():
                preview = sent.strip()[:max_len]
                return preview + "..." if len(sent) > max_len else preview

    return content[:max_len] + "..." if len(content) > max_len else content


def read_note(note_name: str) -> Optional[Dict]:
    """读取笔记内容"""
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
                    "modified": datetime.fromtimestamp(md_file.stat().st_mtime).isoformat(),
                    "tags": extract_tags(content),
                    "wikilinks": extract_wikilinks(content)
                }
            except Exception:
                pass
    return None


# ============== 改进的搜索 ==============

def search_by_title_exact(query: str) -> List[Dict]:
    """标题精确匹配"""
    results = []
    query_lower = query.lower()

    for md_file in VAULT_PATH.rglob("*.md"):
        stem = md_file.stem.lower()
        if query_lower == stem:
            try:
                with open(md_file, 'r', encoding='utf-8') as f:
                    content = f.read(500)
                results.append({
                    "title": extract_title(content, md_file.stem),
                    "path": str(md_file.relative_to(VAULT_PATH)),
                    "score": 100,
                    "match_type": "title_exact"
                })
            except Exception:
                pass

    return results


def search_by_tags(query: str) -> List[Dict]:
    """标签匹配"""
    results = []
    query_lower = query.lower()

    # 扩展同义词
    expanded_queries = [query_lower]
    for key, synonyms in TECH_SYNONYMS.items():
        if query_lower == key or query_lower in [s.lower() for s in synonyms]:
            expanded_queries.append(key)
            expanded_queries.extend([s.lower() for s in synonyms])

    for md_file in VAULT_PATH.rglob("*.md"):
        try:
            with open(md_file, 'r', encoding='utf-8') as f:
                content = f.read(2000)

            tags = extract_tags(content)
            tags_lower = [t.lower() for t in tags]

            # 计算匹配分数
            match_count = sum(1 for q in expanded_queries if q in tags_lower)
            if match_count > 0:
                results.append({
                    "title": extract_title(content, md_file.stem),
                    "path": str(md_file.relative_to(VAULT_PATH)),
                    "tags": tags,
                    "score": 80 + match_count * 5,
                    "match_type": "tag"
                })
        except Exception:
            pass

    return results


def search_by_title_fuzzy(query: str) -> List[Dict]:
    """标题模糊匹配"""
    results = []
    query_lower = query.lower()

    for md_file in VAULT_PATH.rglob("*.md"):
        stem = md_file.stem.lower()
        if query_lower in stem:
            try:
                with open(md_file, 'r', encoding='utf-8') as f:
                    content = f.read(500)
                results.append({
                    "title": extract_title(content, md_file.stem),
                    "path": str(md_file.relative_to(VAULT_PATH)),
                    "score": 70,
                    "match_type": "title_fuzzy"
                })
            except Exception:
                pass

    return results


def search_by_content(query: str, limit: int = 30) -> List[Dict]:
    """全文内容匹配"""
    results = []

    try:
        result = subprocess.run(
            ["rg", "-l", "-i", query, str(VAULT_PATH), "--type", "md"],
            capture_output=True, text=True, timeout=30
        )
        files = result.stdout.strip().split('\n') if result.stdout.strip() else []
    except (FileNotFoundError, subprocess.TimeoutExpired):
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
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read(2000)
            results.append({
                "title": extract_title(content, path.stem),
                "path": str(path.relative_to(VAULT_PATH)),
                "preview": extract_preview(content, query),
                "score": 50,
                "match_type": "content"
            })
        except Exception:
            pass

    return results


def search_notes_v2(query: str, limit: int = 20, preview: bool = True, tag: str = None) -> List[Dict]:
    """改进版搜索 - 多级优先级"""
    if tag:
        # 标签搜索模式
        results = search_by_tags(tag)
    else:
        # 多级搜索
        exact = search_by_title_exact(query)
        tags = search_by_tags(query)
        fuzzy = search_by_title_fuzzy(query)
        content = search_by_content(query)

        # 合并去重
        seen = set()
        all_results = []

        for r in (exact + tags + fuzzy + content):
            if r["title"] not in seen:
                seen.add(r["title"])
                all_results.append(r)

        # 按分数排序
        all_results.sort(key=lambda x: x["score"], reverse=True)
        results = all_results[:limit]

    # 补充预览
    if preview:
        for r in results:
            if "preview" not in r:
                note = read_note(r["title"])
                if note:
                    r["preview"] = extract_preview(note["content"], query)

    return results


# ============== 智能总结 ==============

def summarize_note(note_name: str) -> Dict:
    """LLM 智能总结"""
    note = read_note(note_name)
    if not note:
        return {"error": f"未找到笔记: {note_name}"}

    content = note["content"]
    title = note["title"]

    # 移除 frontmatter
    if content.startswith("---"):
        content = re.sub(r'^---\n.*?\n---\n', '', content, flags=re.DOTALL)

    # 限制长度
    if len(content) > 4000:
        content = content[:4000] + "\n\n... (内容过长已截断)"

    prompt = f"""请总结以下笔记内容「{title}」，输出结构化摘要：

{content}

输出格式：
## 核心观点
- 观点1
- 观点2

## 关键信息
- 信息1
- 信息2

## 行动建议（如有）
- 建议1
"""

    try:
        # 使用 zhiwei_common.llm
        sys.path.insert(0, str(Path.home() / "zhiwei-common"))
        from zhiwei_common.llm import llm_client

        success, summary = llm_client.call("research", prompt, timeout=60)

        if success:
            return {
                "title": title,
                "summary": summary,
                "original_size": note["size"],
                "tags": note.get("tags", []),
                "wikilinks": note.get("wikilinks", [])
            }
        else:
            return {"error": f"LLM 总结失败: {summary}"}

    except Exception as e:
        return {"error": f"总结异常: {str(e)}"}


# ============== 知识关联 ==============

def find_related_notes(note_name: str) -> Dict:
    """查找相关笔记"""
    note = read_note(note_name)
    if not note:
        return {"error": f"未找到笔记: {note_name}"}

    content = note["content"]
    title = note["title"]

    # 1. 提取 wikilinks
    wikilinks = note.get("wikilinks", extract_wikilinks(content))

    # 2. 提取 tags
    tags = note.get("tags", extract_tags(content))

    # 3. 查找同标签笔记
    same_tag_notes = []
    for tag in tags[:5]:  # 限制 5 个标签
        tag_results = search_by_tags(tag)
        for r in tag_results:
            if r["title"] != title and r["title"] not in [n["title"] for n in same_tag_notes]:
                same_tag_notes.append({
                    "title": r["title"],
                    "common_tag": tag
                })

    # 4. 查找反向链接
    backlinks = []
    for md_file in VAULT_PATH.rglob("*.md"):
        try:
            with open(md_file, 'r', encoding='utf-8') as f:
                c = f.read()
            if f"[[{title}" in c or f"[[{title.lower()}" in c.lower():
                backlinks.append({
                    "title": extract_title(c, md_file.stem),
                    "path": str(md_file.relative_to(VAULT_PATH))
                })
        except Exception:
            pass

    return {
        "title": title,
        "wikilinks": wikilinks[:10],
        "tags": tags,
        "same_tag_notes": same_tag_notes[:10],
        "backlinks": backlinks[:10]
    }


# ============== 飞书分享 ==============

def share_to_feishu(note_name: str, mode: str = "summary") -> Dict:
    """分享到飞书"""
    import yaml

    # 1. 读取飞书配置
    config_path = Path.home() / "zhiwei-scheduler" / "config" / "settings.yaml"
    if not config_path.exists():
        return {"error": "飞书配置文件不存在"}

    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    feishu_conf = config.get("push", {}).get("feishu", {})
    if not feishu_conf.get("enabled"):
        return {"error": "飞书推送未启用"}

    # 2. 读取笔记
    note = read_note(note_name)
    if not note:
        return {"error": f"未找到笔记: {note_name}"}

    title = note["title"]

    # 3. 生成内容
    if mode == "summary":
        summary_result = summarize_note(note_name)
        if "error" in summary_result:
            content = note["content"][:500]
        else:
            content = f"# {title}\n\n{summary_result['summary']}"
    else:
        content = note["content"]
        if len(content) > 4000:
            content = content[:3950] + "\n\n... (内容过长已截断)"

    # 4. 推送
    try:
        sys.path.insert(0, str(Path.home() / "zhiwei-common"))
        from zhiwei_common.pusher import FeishuPusher

        pusher = FeishuPusher(
            app_id=feishu_conf["app_id"],
            app_secret=feishu_conf["app_secret"],
            chat_id=feishu_conf["chat_id"]
        )
        result = pusher.send_markdown(f"📝 {title}", content)

        if result.get("code") == 0:
            return {"success": True, "title": title, "mode": mode}
        else:
            return {"error": f"推送失败: {result.get('msg', '未知错误')}"}

    except Exception as e:
        return {"error": f"推送异常: {str(e)}"}


# ============== 其他基础功能 ==============

def create_note(title: str, content: str = "", category: str = "Inbox",
                tags: List[str] = None, status: str = "draft") -> Dict:
    """创建笔记"""
    dir_name = CATEGORIES.get(category, category)
    target_dir = VAULT_PATH / dir_name
    target_dir.mkdir(parents=True, exist_ok=True)

    date_str = datetime.now().strftime("%Y-%m-%d")
    safe_title = "".join(c if c.isalnum() or c in " -_" else "_" for c in title)
    filename = f"NOTE_{date_str}_{safe_title[:50]}.md"
    filepath = target_dir / filename

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

        rel_path = md_file.relative_to(VAULT_PATH)
        category = rel_path.parts[0] if rel_path.parts else "Unknown"
        by_category[category] = by_category.get(category, 0) + 1

    return {
        "vault_path": str(VAULT_PATH),
        "total_notes": total_files,
        "total_size_mb": round(total_size / 1024 / 1024, 2),
        "by_category": dict(sorted(by_category.items(), key=lambda x: -x[1]))
    }


# ============== CLI 入口 ==============

def main():
    parser = argparse.ArgumentParser(
        description="Obsidian CLI v2.0 - 知微 Vault 智能管理工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 搜索（改进版，标题优先）
  obsidian-cli search "Agent" --preview
  obsidian-cli search --tag AI

  # 读取（支持摘要）
  obsidian-cli read "RAG" --summarize

  # 智能总结（新功能）
  obsidian-cli summarize "Transformer"

  # 知识关联（新功能）
  obsidian-cli related "Agent"

  # 飞书分享（新功能）
  obsidian-cli share "RAG" --to feishu --mode summary

  # 其他
  obsidian-cli create "新笔记" --category Personal --tags AI
  obsidian-cli recent --limit 20
  obsidian-cli stats
        """
    )

    subparsers = parser.add_subparsers(dest="command", help="命令")

    # search
    search_parser = subparsers.add_parser("search", help="搜索笔记（改进版）")
    search_parser.add_argument("query", nargs="?", help="搜索关键词")
    search_parser.add_argument("--limit", "-l", type=int, default=20, help="结果数量")
    search_parser.add_argument("--preview", "-p", action="store_true", help="显示摘要预览")
    search_parser.add_argument("--tag", "-t", help="按标签搜索")
    search_parser.add_argument("--json", "-j", action="store_true", help="JSON 输出")

    # read
    read_parser = subparsers.add_parser("read", help="读取笔记")
    read_parser.add_argument("note", help="笔记名称")
    read_parser.add_argument("--summarize", "-s", action="store_true", help="智能总结")

    # summarize (新增)
    summarize_parser = subparsers.add_parser("summarize", help="LLM 智能总结")
    summarize_parser.add_argument("note", help="笔记名称")

    # related (新增)
    related_parser = subparsers.add_parser("related", help="知识关联分析")
    related_parser.add_argument("note", help="笔记名称")

    # share (新增)
    share_parser = subparsers.add_parser("share", help="分享到飞书")
    share_parser.add_argument("note", help="笔记名称")
    share_parser.add_argument("--to", choices=["feishu"], default="feishu", help="目标平台")
    share_parser.add_argument("--mode", "-m", choices=["summary", "full"], default="summary", help="分享模式")

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

    # === search ===
    if args.command == "search":
        if args.tag:
            results = search_notes_v2(query="", limit=args.limit, preview=args.preview, tag=args.tag)
        elif args.query:
            results = search_notes_v2(args.query, args.limit, args.preview)
        else:
            print("请指定搜索关键词或 --tag")
            return

        if args.json:
            print(json.dumps(results, ensure_ascii=False, indent=2))
        else:
            if not results:
                print("未找到匹配的笔记")
            else:
                print(f"找到 {len(results)} 条结果:\n")
                for r in results:
                    print(f"  [[{r['title']}]]")
                    print(f"    路径: {r['path']}")
                    if r.get("preview"):
                        print(f"    预览: {r['preview'][:100]}...")
                    if r.get("tags"):
                        print(f"    标签: {', '.join(r['tags'][:5])}")
                    print()

    # === read ===
    elif args.command == "read":
        if args.summarize:
            result = summarize_note(args.note)
            if "error" in result:
                print(f"❌ {result['error']}")
            else:
                print(f"标题: {result['title']}")
                print(f"原文字数: {result['original_size']}")
                print("\n" + "="*50 + "\n")
                print(result['summary'])
        else:
            result = read_note(args.note)
            if result:
                print(f"标题: {result['title']}")
                print(f"路径: {result['path']}")
                print(f"大小: {result['size']} 字节")
                print(f"修改: {result['modified']}")
                if result.get('tags'):
                    print(f"标签: {', '.join(result['tags'])}")
                print("\n" + "="*50 + "\n")
                print(result['content'])

                # 长笔记提示
                if result['size'] > 1000:
                    print("\n💡 提示: 使用 --summarize 获取智能摘要")
            else:
                print(f"未找到笔记: {args.note}")

    # === summarize (新增) ===
    elif args.command == "summarize":
        result = summarize_note(args.note)
        if "error" in result:
            print(f"❌ {result['error']}")
        else:
            print(f"# {result['title']} - 智能摘要\n")
            print(f"原文字数: {result['original_size']}")
            if result.get('tags'):
                print(f"标签: {', '.join(result['tags'])}")
            if result.get('wikilinks'):
                print(f"引用: {', '.join(result['wikilinks'][:5])}")
            print("\n" + "="*50 + "\n")
            print(result['summary'])

    # === related (新增) ===
    elif args.command == "related":
        result = find_related_notes(args.note)
        if "error" in result:
            print(f"❌ {result['error']}")
        else:
            print(f"# {result['title']} - 知识关联\n")

            if result['wikilinks']:
                print("## 引用的笔记 (Wikilinks)")
                for link in result['wikilinks']:
                    print(f"  - [[{link}]]")
                print()

            if result['tags']:
                print(f"## 标签")
                print(f"  {', '.join(result['tags'])}\n")

            if result['same_tag_notes']:
                print("## 同标签笔记")
                for n in result['same_tag_notes']:
                    print(f"  - [[{n['title']}]] (共标签: {n['common_tag']})")
                print()

            if result['backlinks']:
                print("## 反向链接 (谁引用了我)")
                for n in result['backlinks']:
                    print(f"  - [[{n['title']}]]")
                print()

            if not any([result['wikilinks'], result['same_tag_notes'], result['backlinks']]):
                print("暂未发现关联笔记")

    # === share (新增) ===
    elif args.command == "share":
        print(f"正在分享到飞书...")
        result = share_to_feishu(args.note, args.mode)
        if result.get("success"):
            print(f"✅ 已分享「{result['title']}」到飞书群 ({result['mode']} 模式)")
        else:
            print(f"❌ 分享失败: {result.get('error', '未知错误')}")

    # === create ===
    elif args.command == "create":
        tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else []
        content = args.content or ""
        result = create_note(args.title, content, args.category, tags, args.status)
        print(f"✅ 笔记已创建: {result['path']}")
        print(f"   Wikilink: {result['wikilink']}")

    # === recent ===
    elif args.command == "recent":
        results = list_recent(args.limit)
        print(f"最近 {len(results)} 条笔记:\n")
        for r in results:
            print(f"  [{r['modified']}] [[{r['title']}]]")

    # === stats ===
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