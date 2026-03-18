#!/usr/bin/env python3
"""
Library 文档摘要生成器
为 PDF/EPUB 文件生成 Obsidian 笔记

流程：
1. 读取分类结果
2. 提取文本（PyMuPDF/ebooklib）
3. 判断文档类型（中文研报/英文电子书）
4. 调用 LLM 生成摘要
5. 调用 zhiwei-obsidian 服务导出到 Obsidian

优先使用 zhiwei-obsidian 服务，服务不可用时回退到本地实现。
"""

import os
import re
import sys
import json
import time
from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple

import fitz  # PyMuPDF
from ebooklib import epub

# 添加 zhiwei-bot 路径
sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts.obsidian_summary_filler import generate_ai_summary, get_text_for_summary

# 导入 Obsidian 客户端
try:
    from core.obsidian_client import obsidian_client
    HAS_OBSIDIAN_CLIENT = True
except ImportError:
    HAS_OBSIDIAN_CLIENT = False
    print("⚠️ Obsidian 客户端不可用，使用本地实现")

# ============================================================================
# 配置
# ============================================================================

LIBRARY_BASE = Path.home() / "Documents" / "Library"
VAULT_BASE = Path.home() / "Documents" / "ZhiweiVault"
CLASSIFICATION_FILE = LIBRARY_BASE / "【待整理】" / "_classification.json"

# 中文研报关键词
CN_REPORT_KEYWORDS = ["报告", "白皮书", "研究", "分析", "行业", "市场", "展望", "专题", "解读"]

# ============================================================================
# 文本提取
# ============================================================================

def extract_pdf_text(file_path: Path) -> Tuple[str, int]:
    """提取 PDF 文本，返回 (文本, 页数)"""
    try:
        doc = fitz.open(file_path)
        text_parts = []
        for page in doc:
            text_parts.append(page.get_text())
        text = "\n".join(text_parts)
        pages = len(doc)
        doc.close()
        return text, pages
    except Exception as e:
        print(f"  ❌ PDF 提取失败: {e}")
        return "", 0

def extract_epub_text(file_path: Path) -> Tuple[str, int]:
    """提取 EPUB 文本，返回 (文本, 章节数)"""
    try:
        book = epub.read_epub(str(file_path))
        text_parts = []
        chapter_count = 0
        for item in book.get_items():
            if item.get_type() == 9:  # ITEM_DOCUMENT
                content = item.get_content().decode('utf-8', errors='ignore')
                # 简单去除 HTML 标签
                text = re.sub(r'<[^>]+>', ' ', content)
                text = re.sub(r'\s+', ' ', text)
                if text.strip():
                    text_parts.append(text.strip())
                    chapter_count += 1
        return "\n".join(text_parts), chapter_count
    except Exception as e:
        print(f"  ❌ EPUB 提取失败: {e}")
        return "", 0

def extract_text(file_path: Path) -> Tuple[str, int, str]:
    """提取文本，返回 (文本, 页数/章节数, 文件类型)"""
    suffix = file_path.suffix.lower()
    if suffix == '.pdf':
        text, pages = extract_pdf_text(file_path)
        return text, pages, 'pdf'
    elif suffix == '.epub':
        text, chapters = extract_epub_text(file_path)
        return text, chapters, 'epub'
    else:
        return "", 0, 'unknown'

# ============================================================================
# 文档类型判断
# ============================================================================

def is_chinese_report(filename: str, text: str) -> bool:
    """判断是否为中文研报"""
    # 1. 文件名检查
    for kw in CN_REPORT_KEYWORDS:
        if kw in filename:
            return True

    # 2. 文本开头检查（前 500 字符）
    sample = text[:500]
    chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', sample))
    total_chars = len(sample)

    # 中文占比 > 50% 认为是中文文档
    if total_chars > 0 and chinese_chars / total_chars > 0.5:
        return True

    return False

def get_doc_type_label(filename: str) -> str:
    """获取文档类型标签"""
    if "报告" in filename:
        return "行业研报"
    elif "白皮书" in filename:
        return "产品白皮书"
    elif any(kw in filename for kw in ["研究", "分析"]):
        return "研究报告"
    elif "专题" in filename:
        return "专题研究"
    else:
        return "电子书"

# ============================================================================
# 笔记生成
# ============================================================================

def generate_chinese_report_note(
    title: str,
    summary: str,
    file_path: Path,
    category: str,
    pages: int,
    doc_type: str
) -> str:
    """生成中文研报笔记（AI 硬件架构师模板）"""
    date_str = datetime.now().strftime("%Y-%m-%d")
    rel_path = file_path.relative_to(Path.home() / "Documents")

    return f'''---
title: "{title}"
date: {date_str}
type: {doc_type}
pages: {pages}
category: {category}
source: "[[Library/{rel_path}]]"
tags: [AI, 硬件, 架构]
---

# {title}

> 来源：[[Library/{rel_path}]] | {pages} 页

{summary}

---
> 由知微系统自动生成
'''

def generate_english_book_note(
    title: str,
    summary: str,
    file_path: Path,
    category: str,
    chapters: int,
    doc_type: str
) -> str:
    """生成英文电子书笔记（简化模板）"""
    date_str = datetime.now().strftime("%Y-%m-%d")
    rel_path = file_path.relative_to(Path.home() / "Documents")

    # 简化的摘要
    if summary and not summary.startswith("❌"):
        summary_section = f"\n## 摘要\n\n{summary}\n"
    else:
        summary_section = "\n## 摘要\n\n> 待补充\n"

    return f'''---
title: "{title}"
date: {date_str}
type: {doc_type}
chapters: {chapters}
category: {category}
source: "[[Library/{rel_path}]]"
tags: [AI, 技术, 电子书]
---

# {title}

> 来源：[[Library/{rel_path}]] | {chapters} 章节
{summary_section}

---
> 由知微系统自动生成
'''

# ============================================================================
# 文件名清理
# ============================================================================

def clean_title(filename: str) -> str:
    """从文件名提取标题"""
    # 移除扩展名
    title = Path(filename).stem
    # 移除页数后缀
    title = re.sub(r'\d+页$', '', title)
    # 移除括号内容
    title = re.sub(r'\s*\([^)]*\)\s*', ' ', title)
    title = re.sub(r'\s*\[[^\]]*\]\s*', ' ', title)
    # 清理多余空格
    title = re.sub(r'\s+', ' ', title).strip()
    return title

def safe_filename(title: str) -> str:
    """生成安全的文件名"""
    # 移除不允许的字符
    safe = re.sub(r'[<>:"/\\|?*]', '', title)
    # 限制长度
    if len(safe) > 100:
        safe = safe[:100]
    return safe

# ============================================================================
# 主流程
# ============================================================================

def process_file(file_info: dict, category: str) -> dict:
    """处理单个文件

    优先使用 zhiwei-obsidian 服务，服务不可用时回退到本地实现。
    """
    file_path = Path(file_info["original_path"])

    # 检查文件是否存在
    if not file_path.exists():
        # 尝试在目标目录查找
        target_dir = LIBRARY_BASE / category
        alt_path = target_dir / file_info["cleaned_name"]
        if alt_path.exists():
            file_path = alt_path
        else:
            return {"status": "error", "error": "文件不存在", "file": str(file_path)}

    print(f"\n📄 处理: {file_info['cleaned_name'][:50]}...")

    # 1. 提取文本
    text, pages, file_type = extract_text(file_path)
    if not text:
        return {"status": "error", "error": "文本提取失败", "file": str(file_path)}

    print(f"  ✅ 提取文本: {len(text)} 字符, {pages} {'页' if file_type == 'pdf' else '章节'}")

    # 2. 判断文档类型
    is_cn_report = is_chinese_report(file_info["cleaned_name"], text)
    doc_type = get_doc_type_label(file_info["cleaned_name"])
    title = clean_title(file_info["cleaned_name"])

    print(f"  📝 类型: {'中文研报' if is_cn_report else '英文电子书'} | {doc_type}")

    # 3. 生成摘要
    summary = ""
    print(f"  🤖 生成 AI 摘要...")
    truncated = get_text_for_summary(text)
    summary = generate_ai_summary(truncated, doc_type)
    if summary.startswith("❌"):
        print(f"  ⚠️ 摘要生成失败: {summary}")
    else:
        print(f"  ✅ 摘要生成成功: {len(summary)} 字符")

    # 4. 尝试使用 zhiwei-obsidian 服务导出
    if HAS_OBSIDIAN_CLIENT and obsidian_client.is_available():
        print(f"  📤 使用 zhiwei-obsidian 服务导出...")
        result = obsidian_client.export_report(
            title=title,
            summary=summary,
            source=str(file_path),
            pages=pages,
            doc_type=doc_type,
        )

        if result.get("success"):
            print(f"  ✅ 服务导出成功: {Path(result['md_path']).name}")
            return {
                "status": "success",
                "file": str(file_path),
                "title": title,
                "note": result.get("md_path", ""),
                "pages": pages,
                "is_chinese": is_cn_report,
                "summary_length": len(summary) if summary else 0,
                "export_method": "service"
            }
        else:
            print(f"  ⚠️ 服务导出失败: {result.get('error')}，回退到本地实现")

    # 5. 本地实现：生成并保存笔记
    if is_cn_report:
        note_content = generate_chinese_report_note(
            title, summary, file_path, category, pages, doc_type
        )
    else:
        note_content = generate_english_book_note(
            title, summary, file_path, category, pages, doc_type
        )

    # 6. 保存笔记
    date_prefix = datetime.now().strftime("%Y%m%d")
    safe_title = safe_filename(title)
    note_filename = f"{date_prefix}_{safe_title}.md"

    # 确定输出目录
    output_dir = VAULT_BASE / category
    output_dir.mkdir(parents=True, exist_ok=True)

    note_path = output_dir / note_filename

    # 避免覆盖
    counter = 1
    while note_path.exists():
        note_path = output_dir / f"{date_prefix}_{safe_title}_{counter}.md"
        counter += 1

    note_path.write_text(note_content, encoding='utf-8')
    print(f"  ✅ 笔记已保存: {note_path.name}")

    return {
        "status": "success",
        "file": str(file_path),
        "title": title,
        "note": str(note_path),
        "pages": pages,
        "is_chinese": is_cn_report,
        "summary_length": len(summary) if summary else 0
    }

def main():
    """主函数"""
    print("=" * 60)
    print("📚 Library 文档摘要生成器")
    print("=" * 60)

    # 读取分类结果
    if not CLASSIFICATION_FILE.exists():
        print(f"❌ 分类文件不存在: {CLASSIFICATION_FILE}")
        return

    with open(CLASSIFICATION_FILE, encoding='utf-8') as f:
        classification = json.load(f)

    # 统计
    total = sum(len(files) for files in classification.values())
    print(f"\n📊 待处理文件: {total} 个")
    for cat, files in classification.items():
        print(f"  - {cat}: {len(files)} 个")

    # 处理结果
    results = {
        "success": [],
        "error": [],
        "stats": {
            "total": total,
            "chinese_reports": 0,
            "english_books": 0,
            "total_pages": 0
        }
    }

    # 按分类处理
    for category, files in classification.items():
        print(f"\n{'=' * 60}")
        print(f"📁 处理分类: {category}")
        print("=" * 60)

        for file_info in files:
            result = process_file(file_info, category)

            if result["status"] == "success":
                results["success"].append(result)
                results["stats"]["total_pages"] += result["pages"]
                if result["is_chinese"]:
                    results["stats"]["chinese_reports"] += 1
                else:
                    results["stats"]["english_books"] += 1
            else:
                results["error"].append(result)

            # 避免 API 限流
            time.sleep(1)

    # 输出报告
    print("\n" + "=" * 60)
    print("📋 处理报告")
    print("=" * 60)
    print(f"✅ 成功处理: {len(results['success'])} 个")
    print(f"❌ 处理失败: {len(results['error'])} 个")
    print(f"📊 中文研报: {results['stats']['chinese_reports']} 个")
    print(f"📊 英文电子书: {results['stats']['english_books']} 个")
    print(f"📄 总页数/章节: {results['stats']['total_pages']}")

    if results["error"]:
        print("\n❌ 失败列表:")
        for err in results["error"]:
            print(f"  - {Path(err['file']).name}: {err['error']}")

    # 保存处理结果
    result_file = LIBRARY_BASE / "【待整理】" / "_processing_result.json"
    with open(result_file, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n💾 结果已保存: {result_file}")

if __name__ == "__main__":
    main()