#!/usr/bin/env python3
"""
T-071 技术对比模块
支持 /对比 命令：生成技术对比表格

用法:
  python3 tech_compare.py "React vs Vue"
  python3 tech_compare.py "PostgreSQL 与 MySQL"
"""

import sys
import os
import subprocess
from pathlib import Path

# 添加 Library 目录到路径
sys.path.insert(0, str(Path.home() / "Documents" / "Library"))
sys.path.insert(0, str(Path.home() / "zhiwei-scheduler"))

from klib_hybrid import hybrid_search


def parse_comparison(text: str) -> tuple[str, str]:
    """
    解析对比语句，提取两个对比项

    支持格式：
    - A vs B / A VS B
    - A 与 B / A 和 B / A 以及 B
    """
    # 转小写进行匹配
    text_lower = text.lower().strip()

    # 尝试各种分隔符
    separators = [" vs ", " VS ", " VS ", " vs ", " 与 ", " 和 ", " 以及 ", "对比", "vs"]

    for sep in separators:
        if sep.lower() in text_lower:
            parts = text.split(sep)
            if len(parts) >= 2:
                item_a = parts[0].strip()
                item_b = parts[1].strip()
                return item_a, item_b

    # 如果没找到分隔符，尝试按空格分割
    words = text.split()
    if len(words) >= 3:
        # 取前两个单词作为对比项
        return words[0], words[1]

    return text, ""


def retrieve_info(item: str) -> list[dict]:
    """
    从知识库检索相关信息

    Args:
        item: 搜索项

    Returns:
        检索结果列表
    """
    print(f"🔍 检索: {item}")

    results = hybrid_search(item, top_k=2)

    if not results:
        print(f"   ⚠️  未找到相关知识库内容")
        return []

    print(f"   ✅ 找到 {len(results)} 篇相关文章")

    # 返回整理后的信息
    formatted = []
    for doc in results:
        info = {
            "title": doc.get("title", "未知"),
            "summary": doc.get("summary", "")[:200],
            "category": doc.get("category", ""),
            "priority": doc.get("priority", ""),
        }
        formatted.append(info)

    return formatted


def generate_comparison(item_a: str, item_b: str, context: dict) -> str:
    """
    调用 LLM 生成对比

    Args:
        item_a: 对比项A
        item_b: 对比项B
        context: 上下文信息

    Returns:
        生成的对比Markdown
    """
    print("📊 调用 LLM 生成对比...")

    # 构建引导信息
    info_a = context.get("item_a_info", [])
    info_b = context.get("item_b_info", [])

    knowledge_section = ""
    if info_a:
        knowledge_section += f"\n## {item_a} 相关知识库资料\n"
        for doc in info_a:
            knowledge_section += f"- **{doc.get('title', '未知')}**\n"
            if doc.get('summary'):
                knowledge_section += f"  {doc.get('summary')[:80]}\n"
    if info_b:
        knowledge_section += f"\n## {item_b} 相关知识库资料\n"
        for doc in info_b:
            knowledge_section += f"- **{doc.get('title', '未知')}**\n"
            if doc.get('summary'):
                knowledge_section += f"  {doc.get('summary')[:80]}\n"

    prompt = f"""你是一位专业的技术分析师。请对以下两个项目进行详细对比分析。

## 对比目标
- 项目A: {item_a}
- 项目B: {item_b}

## 输出要求

### 1. 对比表格（必须包含至少5个维度）
使用 Markdown 表格格式，维度应该合理：
- 技术类：性能、生态、学习曲线、适用场景、优缺点、社区活跃度
- 产品类：价格、功能、用户群、市场份额、支持
- 通用类：核心特点、差异、相似之处、发展趋势

### 2. 详细分析
分别说明两个项目的优劣势：

### {item_a} 优势
[列出 3-5 条主要优势]

### {item_b} 优势
[列出 3-5 条主要优势]

### 3. 结论
基于对比给出选择建议。

## 知识库参考（可选参考，不要直接引用）
{knowledge_section}

## 输出格式示例

📊 对比：{item_a} vs {item_b}

| 维度 | {item_a} | {item_b} |
|------|----------|----------|
| 维度1 | ... | ... |
| 维度2 | ... | ... |

## 详细分析

### {item_a} 优势
- ...

### {item_b} 优势
- ...

## 结论
...

---
参考了 X 篇知识库文档
"""

    # 调用 openclaw agent
    container = "clawdbot"
    # 去除 prompt 中的换行符和特殊字符
    safe_prompt = prompt.replace("\n", " ").replace('"', '\\"')[:2000]

    try:
        cmd = f'docker exec {container} openclaw agent --agent researcher --message "{safe_prompt}"'
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=90)

        if result.returncode == 0:
            comparison = result.stdout.strip()
            print("   ✅ 对比生成成功")
            return comparison
        else:
            print(f"   ❌ 生成失败: {result.stderr[:200]}")
            return f"# 对比：{item_a} vs {item_b}\n\n生成失败：{result.stderr[:200]}"

    except subprocess.TimeoutExpired:
        print("   ❌ 生成超时")
        return f"# 对比：{item_a} vs {item_b}\n\n生成超时（超过90秒）"
    except Exception as e:
        print(f"   ❌ 生成异常: {e}")
        return f"# 对比：{item_a} vs {item_b}\n\n生成异常：{str(e)}"


def format_comparison(item_a: str, item_b: str, content: str, context: dict) -> str:
    """
    格式化输出

    Args:
        item_a: 对比项A
        item_b: 对比项B
        content: 内容
        context: 上下文信息

    Returns:
        格式化的完整对比
    """
    # 统计引用数量
    info_a = context.get("item_a_info", [])
    info_b = context.get("item_b_info", [])
    total_refs = len(info_a) + len(info_b)

    # 添加参考来源统计
    article = f"""{content}

---
📚 参考了 {total_refs} 篇知识库文档
"""
    return article


def compare_tech(item_a: str, item_b: str, user_id: str = None) -> str:
    """
    主函数：技术对比

    Args:
        item_a: 对比项A
        item_b: 对比项B
        user_id: 用户ID（用于主动推送结果，可选）

    Returns:
        生成的对比（完整 Markdown 格式）
    """
    print(f"📊 开始对比：{item_a} vs {item_b}")
    print("=" * 50)

    # 1. 检索知识库
    print(f"🔍 检索知识库...")
    info_a = retrieve_info(item_a)
    info_b = retrieve_info(item_b)

    # 2. 构建上下文
    context = {
        "item_a_info": info_a,
        "item_b_info": info_b
    }

    # 3. 生成对比
    content = generate_comparison(item_a, item_b, context)

    # 4. 格式化输出
    comparison = format_comparison(item_a, item_b, content, context)

    print("=" * 50)
    print(f"📊 对比完成")

    # 如果提供了用户ID，通过飞书主动推送结果
    if user_id:
        try:
            from feishu_api import send_direct_message
            success = send_direct_message(user_id, comparison)
            if success:
                print(f"✅ 对比结果已推送给用户 {user_id}")
            else:
                print(f"❌ 对比结果推送失败给用户 {user_id}")
        except ImportError:
            print("⚠️ 无法导入 send_direct_message，无法推送结果")
        except Exception as e:
            print(f"❌ 推送时发生异常: {e}")

    return comparison


def main():
    if len(sys.argv) < 2:
        print("用法: python3 tech_compare.py '技术A vs 技术B'")
        print("示例: python3 tech_compare.py 'React vs Vue'")
        print("支持分隔符: vs, VS, 与, 和, 以及")
        sys.exit(1)

    text = " ".join(sys.argv[1:])
    item_a, item_b = parse_comparison(text)

    if not item_b:
        print(f"❌ 无法解析对比项，请使用 'A vs B' 格式")
        print(f"当前输入: {text}")
        sys.exit(1)

    print(f"对比项A: {item_a}")
    print(f"对比项B: {item_b}")
    print()

    comparison = compare_tech(item_a, item_b)
    print("\n" + comparison)


if __name__ == "__main__":
    main()
