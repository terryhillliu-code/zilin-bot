#!/usr/bin/env python3
"""
技术分析 CLI - 结合 Obsidian Vault 进行深度技术分析

功能:
- analyze: 技术主题深度分析
- compare: 技术对比分析
- trend: 技术趋势分析
- related: 相关技术关联

用法:
    tech-cli analyze <技术主题>
    tech-cli compare <技术A> <技术B>
    tech-cli trend <技术领域>
    tech-cli related <技术主题>
"""
import sys
import os
import json
import argparse
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict

# Vault 路径
VAULT_PATH = Path.home() / "Documents" / "ZhiweiVault"


def search_tech_notes(keyword: str, limit: int = 20) -> List[Dict]:
    """搜索技术相关笔记"""
    results = []

    try:
        # 使用 ripgrep 搜索
        result = subprocess.run(
            ["rg", "-l", "-i", keyword, str(VAULT_PATH), "--type", "md"],
            capture_output=True, text=True, timeout=30
        )
        files = result.stdout.strip().split('\n') if result.stdout.strip() else []
    except (FileNotFoundError, subprocess.TimeoutExpired):
        # 降级到 grep
        result = subprocess.run(
            ["grep", "-r", "-l", "-i", keyword, str(VAULT_PATH), "--include=*.md"],
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
                title = extract_title(content, path.stem)

            # 提取技术关键词
            keywords = extract_tech_keywords(content)

            # 提取时间
            mtime = datetime.fromtimestamp(path.stat().st_mtime)

            results.append({
                "title": title,
                "path": str(path.relative_to(VAULT_PATH)),
                "keywords": keywords,
                "modified": mtime.strftime("%Y-%m-%d"),
                "content_preview": content[:500]
            })
        except Exception:
            pass

    return results


def extract_title(content: str, fallback: str) -> str:
    """从内容中提取标题"""
    import re

    # 从 frontmatter 提取
    if content.startswith("---"):
        match = re.search(r'^title:\s*["\']?(.+?)["\']?\s*$', content, re.MULTILINE)
        if match:
            return match.group(1).strip('"\'')

    # 从第一个 # 标题提取
    match = re.search(r'^#\s+(.+)$', content, re.MULTILINE)
    if match:
        return match.group(1).strip()

    return fallback


def extract_tech_keywords(content: str) -> List[str]:
    """提取技术关键词"""
    import re

    # 常见技术关键词模式
    tech_patterns = [
        r'\b(AI|LLM|RAG|Agent|Transformer|GPT|BERT|Claude|GPT-4|Claude-3)\b',
        r'\b(MoE|LoRA|PEFT|RLHF|DPO|SFT)\b',
        r'\b(PyTorch|TensorFlow|JAX|ONNX|CUDA)\b',
        r'\b(Kubernetes|Docker|Microservice|API|REST|gRPC)\b',
        r'\b(Vector|Embedding|LanceDB|Pinecone|Milvus)\b',
        r'\b(React|Vue|Angular|Node\.js|Python|Go|Rust)\b',
    ]

    keywords = set()
    for pattern in tech_patterns:
        matches = re.findall(pattern, content, re.IGNORECASE)
        keywords.update(m.lower() for m in matches)

    return list(keywords)[:10]


def analyze_tech(topic: str) -> Dict:
    """技术主题深度分析"""
    notes = search_tech_notes(topic, limit=30)

    if not notes:
        return {
            "topic": topic,
            "notes_found": 0,
            "analysis": f"在 Vault 中未找到关于 '{topic}' 的笔记"
        }

    # 收集所有关键词
    all_keywords = []
    for note in notes:
        all_keywords.extend(note.get("keywords", []))

    # 统计关键词频率
    from collections import Counter
    keyword_freq = Counter(all_keywords)

    # 按时间排序笔记
    notes_sorted = sorted(notes, key=lambda x: x["modified"], reverse=True)

    # 提取最近的笔记
    recent_notes = notes_sorted[:5]

    # 构建 LLM 分析提示
    notes_context = "\n\n".join([
        f"### {n['title']}\n{n['content_preview'][:300]}..."
        for n in recent_notes[:3]
    ])

    analysis_prompt = f"""基于以下笔记内容，分析技术主题「{topic}」:

{notes_context}

请从以下角度分析:
1. 核心概念定义
2. 技术架构/原理
3. 应用场景
4. 优缺点
5. 发展趋势

输出格式: Markdown
"""

    # 调用 LLM 分析
    try:
        from zhiwei_common.llm import llm_client
        success, analysis = llm_client.call("researcher", analysis_prompt, timeout=120)
        if not success:
            analysis = "LLM 分析失败: " + analysis
    except Exception as e:
        analysis = f"LLM 调用异常: {e}"

    return {
        "topic": topic,
        "notes_found": len(notes),
        "related_keywords": dict(keyword_freq.most_common(10)),
        "recent_notes": [{"title": n["title"], "modified": n["modified"]} for n in recent_notes],
        "analysis": analysis
    }


def compare_techs(tech_a: str, tech_b: str) -> Dict:
    """技术对比分析"""
    notes_a = search_tech_notes(tech_a, limit=10)
    notes_b = search_tech_notes(tech_b, limit=10)

    # 构建 LLM 对比提示
    context_a = "\n".join([f"- {n['title']}: {n['content_preview'][:200]}" for n in notes_a[:3]])
    context_b = "\n".join([f"- {n['title']}: {n['content_preview'][:200]}" for n in notes_b[:3]])

    compare_prompt = f"""对比分析两个技术:

## {tech_a}
{context_a if context_a else "未找到相关笔记"}

## {tech_b}
{context_b if context_b else "未找到相关笔记"}

请从以下维度对比:
| 维度 | {tech_a} | {tech_b} |
|------|---------|---------|
| 核心原理 | | |
| 性能特点 | | |
| 适用场景 | | |
| 学习曲线 | | |
| 生态成熟度 | | |

并给出选择建议。
"""

    try:
        from zhiwei_common.llm import llm_client
        success, comparison = llm_client.call("researcher", compare_prompt, timeout=120)
        if not success:
            comparison = "LLM 分析失败: " + comparison
    except Exception as e:
        comparison = f"LLM 调用异常: {e}"

    return {
        "tech_a": {"name": tech_a, "notes": len(notes_a)},
        "tech_b": {"name": tech_b, "notes": len(notes_b)},
        "comparison": comparison
    }


def analyze_trend(field: str) -> Dict:
    """技术趋势分析"""
    notes = search_tech_notes(field, limit=50)

    if not notes:
        return {
            "field": field,
            "notes_found": 0,
            "trend": f"未找到 '{field}' 相关笔记"
        }

    # 按时间分组
    from collections import defaultdict
    by_month = defaultdict(list)
    for note in notes:
        month = note["modified"][:7]  # YYYY-MM
        by_month[month].append(note)

    # 提取所有关键词并分析趋势
    from collections import Counter
    all_keywords = []
    for note in notes:
        all_keywords.extend(note.get("keywords", []))

    keyword_freq = Counter(all_keywords)

    # 构建趋势分析提示
    recent_notes = sorted(notes, key=lambda x: x["modified"], reverse=True)[:5]
    recent_context = "\n".join([f"- [{n['modified']}] {n['title']}" for n in recent_notes])

    trend_prompt = f"""分析技术领域「{field}」的发展趋势:

## 最近相关笔记
{recent_context}

## 热门关键词
{dict(keyword_freq.most_common(15))}

## 月度分布
{dict(sorted(by_month.items(), reverse=True)[:6])}

请分析:
1. 当前热点
2. 技术演进方向
3. 值得关注的新兴技术
4. 建议深入学习的内容
"""

    try:
        from zhiwei_common.llm import llm_client
        success, trend = llm_client.call("researcher", trend_prompt, timeout=120)
        if not success:
            trend = "LLM 分析失败: " + trend
    except Exception as e:
        trend = f"LLM 调用异常: {e}"

    return {
        "field": field,
        "notes_found": len(notes),
        "hot_keywords": dict(keyword_freq.most_common(10)),
        "monthly_distribution": {k: len(v) for k, v in sorted(by_month.items(), reverse=True)[:6]},
        "trend_analysis": trend
    }


def find_related(topic: str) -> Dict:
    """查找相关技术"""
    notes = search_tech_notes(topic, limit=20)

    if not notes:
        return {"topic": topic, "related": []}

    # 收集所有关键词
    all_keywords = []
    for note in notes:
        all_keywords.extend(note.get("keywords", []))

    from collections import Counter
    keyword_freq = Counter(all_keywords)

    # 排除原主题
    related = [(k, c) for k, c in keyword_freq.most_common(15)
               if k.lower() != topic.lower()]

    # 对每个相关技术搜索笔记数量
    related_with_count = []
    for kw, freq in related[:10]:
        related_notes = search_tech_notes(kw, limit=5)
        related_with_count.append({
            "keyword": kw,
            "co_occurrence": freq,
            "notes": len(related_notes)
        })

    return {
        "topic": topic,
        "notes_analyzed": len(notes),
        "related_technologies": related_with_count
    }


def main():
    parser = argparse.ArgumentParser(
        description="技术分析 CLI - 结合 Obsidian Vault 进行深度技术分析",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  tech-cli analyze "Transformer"
  tech-cli compare "RAG" "GraphRAG"
  tech-cli trend "LLM"
  tech-cli related "Agent"
        """
    )

    subparsers = parser.add_subparsers(dest="command", help="命令")

    # analyze
    analyze_parser = subparsers.add_parser("analyze", help="技术主题深度分析")
    analyze_parser.add_argument("topic", help="技术主题")

    # compare
    compare_parser = subparsers.add_parser("compare", help="技术对比分析")
    compare_parser.add_argument("tech_a", help="技术 A")
    compare_parser.add_argument("tech_b", help="技术 B")

    # trend
    trend_parser = subparsers.add_parser("trend", help="技术趋势分析")
    trend_parser.add_argument("field", help="技术领域")

    # related
    related_parser = subparsers.add_parser("related", help="查找相关技术")
    related_parser.add_argument("topic", help="技术主题")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    if args.command == "analyze":
        result = analyze_tech(args.topic)
        print(f"\n# 技术分析: {args.topic}")
        print(f"\n找到 {result['notes_found']} 条相关笔记")
        if result.get('related_keywords'):
            print(f"\n## 相关关键词")
            for kw, count in result['related_keywords'].items():
                print(f"  - {kw}: {count}")
        print(f"\n## 深度分析\n")
        print(result['analysis'])

    elif args.command == "compare":
        result = compare_techs(args.tech_a, args.tech_b)
        print(f"\n# 技术对比: {result['tech_a']['name']} vs {result['tech_b']['name']}")
        print(f"\n{result['tech_a']['name']}: {result['tech_a']['notes']} 条笔记")
        print(f"{result['tech_b']['name']}: {result['tech_b']['notes']} 条笔记")
        print(f"\n## 对比分析\n")
        print(result['comparison'])

    elif args.command == "trend":
        result = analyze_trend(args.field)
        print(f"\n# 技术趋势: {args.field}")
        print(f"\n找到 {result['notes_found']} 条相关笔记")
        if result.get('hot_keywords'):
            print(f"\n## 热门关键词")
            for kw, count in result['hot_keywords'].items():
                print(f"  - {kw}: {count}")
        print(f"\n## 趋势分析\n")
        print(result['trend_analysis'])

    elif args.command == "related":
        result = find_related(args.topic)
        print(f"\n# 相关技术: {args.topic}")
        print(f"\n分析了 {result['notes_analyzed']} 条笔记")
        print(f"\n## 相关技术")
        for r in result['related_technologies']:
            print(f"  - {r['keyword']}: 共现 {r['co_occurrence']} 次, {r['notes']} 条笔记")


if __name__ == "__main__":
    main()