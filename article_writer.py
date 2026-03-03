#!/usr/bin/env python3
"""
/T-069 文章写作模块
支持 /写稿 命令：从知识库检索 + LLM 生成文章

用法:
  python3 article_writer.py "AI Agent 技术趋势"
"""

import sys
import os
import subprocess
import re
from pathlib import Path

# 添加 Library 目录到路径
sys.path.insert(0, str(Path.home() / "Documents" / "Library"))
sys.path.insert(0, str(Path.home() / "zhiwei-scheduler"))

from klib_hybrid import hybrid_search


def retrieve_from_knowledge_base(topic: str, top_k: int = 3) -> list[dict]:
    """
    从知识库检索相关内容

    Args:
        topic: 搜索主题
        top_k: 返回结果数量

    Returns:
        检索结果列表
    """
    print(f"🔍 知识库检索: {topic}")
    results = hybrid_search(topic, top_k=top_k)

    if not results:
        print("   ⚠️  未找到相关知识库内容")
        return []

    print(f"   ✅ 找到 {len(results)} 篇相关文章")
    return results


def retrieve_from_web(topic: str) -> list[str]:
    """
    从网络搜索补充信息（暂时 skip，待 web-summary 支持搜索）

    Args:
        topic: 搜索主题

    Returns:
        空列表（暂未实现）
    """
    print(f"🌐 网络搜索: (功能暂未启用)")
    return []


def generate_article(topic: str, context: dict) -> str:
    """
    调用 LLM 生成文章

    Args:
        topic: 文章主题
        context: 上下文信息（知识库检索结果）

    Returns:
        生成的 Markdown 文章
    """
    print("📝 调用 LLM 生成文章...")

    # 构建 prompt
    knowledge_docs = context.get("knowledge_base", [])
    web_results = context.get("web_results", [])

    # 构建知识库引用
    knowledge_section = ""
    if knowledge_docs:
        knowledge_section = "\n## 知识库参考\n"
        for doc in knowledge_docs:
            knowledge_section += f"- {doc.get('title', '未知')} \n"
            if doc.get('summary'):
                knowledge_section += f"  > {doc.get('summary')[:100]}...\n"

    # 构建网络搜索引用
    web_section = ""
    if web_results:
        web_section = "\n## 网络搜索参考\n"
        for i, web in enumerate(web_results, 1):
            web_section += f"- {web}\n"

    prompt = f"""你是一位专业的中文内容创作者。请根据以下资料撰写一篇关于「{topic}」的文章。

## 写作要求
- 字数：800-1500 字
- 格式：Markdown
- 语气：专业但易懂，避免过于学术化
- 结构：包含概述、核心观点、详细分析、总结

## 写作步骤
1. 阅读所有参考资料
2. 提取核心信息点
3. 组织文章结构
4. 撰写初稿

{'## 知识库资料' + knowledge_section if knowledge_section else ''}
{'## 网络信息' + web_section if web_section else ''}

请开始撰写文章：
"""

    # 调用 openclaw agent
    container = "clawdbot"
    # 去除 prompt 中的换行符和特殊字符
    safe_prompt = prompt.replace("\n", " ").replace('"', '\\"')[:2000]

    try:
        cmd = f'docker exec {container} openclaw agent --agent researcher --message "{safe_prompt}"'
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=180)

        if result.returncode == 0:
            article = result.stdout.strip()
            print("   ✅ 文章生成成功")
            return article
        else:
            print(f"   ❌ 生成失败: {result.stderr[:200]}")
            return f"# {topic}\n\n生成失败：{result.stderr[:200]}"

    except subprocess.TimeoutExpired:
        print("   ❌ 生成超时")
        return f"# {topic}\n\n生成超时（超过60秒）"
    except Exception as e:
        print(f"   ❌ 生成异常: {e}")
        return f"# {topic}\n\n生成异常：{str(e)}"


def format_article(topic: str, content: str, context: dict) -> str:
    """
    格式化文章输出

    Args:
        topic: 文章主题
        content: 文章内容
        context: 上下文信息

    Returns:
        格式化后的完整文章
    """
    knowledge_docs = context.get("knowledge_base", [])
    web_results = context.get("web_results", [])

    # 统计引用数量
    knowledge_count = len(knowledge_docs)
    web_count = len(web_results)

    # 构建引用来源部分
    sources = []
    for doc in knowledge_docs:
        title = doc.get('title', '未知')
        sources.append(f"- {title}")

    for i, web in enumerate(web_results, 1):
        sources.append(f"- 网络信息 {i}: {web[:50]}...")

    # 添加到文章末尾
    if sources:
        article = f"""# {topic}

{content}

---

## 参考来源
{chr(10).join(sources)}

---
📚 引用了 {knowledge_count} 篇知识库文档 + {web_count} 条网络搜索结果
"""
    else:
        article = f"""# {topic}

{content}

---

## 参考来源
（本次搜索未找到相关参考资料）

---
📚 引用了 {knowledge_count} 篇知识库文档 + {web_count} 条网络搜索结果
"""

    return article


def write_article(topic: str, user_id: str = None) -> str:
    """
    主函数：写稿

    Args:
        topic: 文章主题
        user_id: 用户ID（用于主动推送结果，可选）

    Returns:
        生成的文章（完整 Markdown 格式）
    """
    print(f"📝 开始撰写「{topic}」...")
    print("=" * 50)

    # 1. 检索知识库
    knowledge_results = retrieve_from_knowledge_base(topic, top_k=3)

    # 2. 网络搜索补充
    web_results = retrieve_from_web(topic)

    # 3. 构建上下文
    context = {
        "knowledge_base": knowledge_results,
        "web_results": web_results
    }

    # 4. 生成文章
    content = generate_article(topic, context)

    # 5. 格式化输出
    article = format_article(topic, content, context)

    print("=" * 50)
    print(f"📝 文章撰写完成")

    # 如果提供了用户ID，通过飞书主动推送结果
    if user_id:
        try:
            from feishu_api import send_direct_message
            success = send_direct_message(user_id, article)
            if success:
                print(f"✅ 文章已推送给用户 {user_id}")
            else:
                print(f"❌ 文章推送失败给用户 {user_id}")
                # 如果推送失败，也可以返回文章供调用方处理
        except ImportError:
            print("⚠️ 无法导入 send_direct_message，无法推送结果")
        except Exception as e:
            print(f"❌ 推送时发生异常: {e}")

    return article


def main():
    if len(sys.argv) < 2:
        print("用法: python3 article_writer.py <主题>")
        print("示例: python3 article_writer.py AI Agent 技术趋势")
        sys.exit(1)

    topic = " ".join(sys.argv[1:])
    article = write_article(topic)
    print("\n" + article)


if __name__ == "__main__":
    main()
