#!/usr/bin/env python3
"""
Obsidian 笔记摘要批量填充工具

用法:
    python obsidian_batch_summary.py --dry-run    # 预览模式，不实际写入
    python obsidian_batch_summary.py --limit 10   # 只处理前 10 个
    python obsidian_batch_summary.py --all        # 处理所有缺失摘要的笔记
"""

import os
import re
import sys
import time
import argparse
from pathlib import Path
from datetime import datetime

# 添加脚本目录到 path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from obsidian_summary_filler import generate_ai_summary, get_text_for_summary

# 配置
VAULT_PATH = Path.home() / "Documents" / "ZhiweiVault" / "30_Knowledge_Base"
SOURCE_BASE = Path.home() / "Documents" / "Library"
PLACEHOLDER = "*(等待向量化及摘要提取后自动写入)*"
SUMMARY_MARKER = "## AI 深度摘要"

def find_notes_missing_summary():
    """找出缺少摘要的笔记"""
    missing = []
    for category_dir in VAULT_PATH.iterdir():
        if not category_dir.is_dir():
            continue
        for note_file in category_dir.glob("*.md"):
            try:
                content = note_file.read_text(encoding="utf-8")
                # 检查是否有占位符
                if PLACEHOLDER in content:
                    missing.append(note_file)
                # 或者缺少结构化摘要
                elif "### 核心主题" not in content and "### 1. 核心主题" not in content:
                    # 确保有 AI 深度摘要章节可以插入
                    if SUMMARY_MARKER in content:
                        missing.append(note_file)
            except Exception as e:
                print(f"⚠️ 读取失败: {note_file.name} - {e}")
    return missing

def extract_source_path(note_content: str) -> str:
    """从笔记中提取原始文件路径"""
    # 匹配格式: file:///Users/liufang/Documents/Library/...
    match = re.search(r'file://(/Users/liufang/Documents/Library/[^)]+)', note_content)
    if match:
        return match.group(1)
    return None

def get_source_content(source_path: str) -> str:
    """读取原始文件内容"""
    path = Path(source_path)
    if not path.exists():
        # 尝试解码 URL 编码
        try:
            from urllib.parse import unquote
            decoded_path = unquote(source_path)
            path = Path(decoded_path)
        except:
            pass

    if not path.exists():
        return None

    # 支持 .md 文件
    if path.suffix == ".md":
        try:
            return path.read_text(encoding="utf-8")
        except:
            return None
    elif path.suffix == ".pdf":
        # 尝试查找对应的 .md 提取文件
        # 格式: [年份]_文件名.pdf -> 同目录下 [年份]_文件名.md
        md_path = path.with_suffix(".md")
        if md_path.exists():
            try:
                return md_path.read_text(encoding="utf-8")
            except:
                pass
        # PDF 无法直接读取，返回 None
        return None

    return None

def generate_and_insert_summary(note_file: Path, dry_run: bool = False) -> bool:
    """为单个笔记生成并插入摘要"""
    try:
        note_content = note_file.read_text(encoding="utf-8")

        # 提取原始文件路径
        source_path = extract_source_path(note_content)
        if not source_path:
            print(f"  ⚠️ 无法提取原始路径: {note_file.name}")
            return False

        # 读取原始内容
        source_content = get_source_content(source_path)
        if not source_content:
            print(f"  ⚠️ 无法读取原始文件: {Path(source_path).name}")
            return False

        # 截断文本
        truncated = get_text_for_summary(source_content)

        # 提取标题
        title = note_file.stem

        print(f"  🤖 生成摘要: {title[:40]}...")

        # 生成摘要
        summary = generate_ai_summary(truncated)
        if summary.startswith("❌"):
            print(f"  ❌ 摘要生成失败: {summary}")
            return False

        # 构建新的摘要章节
        new_summary_section = f"{SUMMARY_MARKER}\n## 结构化摘要\n\n{summary}\n"

        if dry_run:
            print(f"  ✅ [预览] 将写入摘要: {len(summary)} 字符")
            return True

        # 根据情况决定如何插入
        if PLACEHOLDER in note_content:
            # 替换占位符
            new_content = note_content.replace(
                f"{SUMMARY_MARKER}\n{PLACEHOLDER}",
                new_summary_section
            )
        elif SUMMARY_MARKER in note_content:
            # 在 AI 深度摘要章节后追加
            # 找到 AI 深度摘要章节的位置
            lines = note_content.split("\n")
            insert_idx = -1
            for i, line in enumerate(lines):
                if line.strip() == SUMMARY_MARKER:
                    # 找到下一个 ## 开头的行，在此之前插入
                    for j in range(i + 1, len(lines)):
                        if lines[j].startswith("##"):
                            insert_idx = j
                            break
                    if insert_idx == -1:
                        insert_idx = len(lines)
                    break

            if insert_idx > 0:
                new_content = "\n".join(lines[:insert_idx]) + "\n" + new_summary_section + "\n".join(lines[insert_idx:])
            else:
                # 追加到文件末尾
                new_content = note_content + "\n\n" + new_summary_section
        else:
            print(f"  ⚠️ 无法确定插入位置")
            return False

        # 写入文件
        note_file.write_text(new_content, encoding="utf-8")
        print(f"  ✅ 已写入摘要: {len(summary)} 字符")
        return True

    except Exception as e:
        print(f"  ❌ 处理失败: {e}")
        return False

def main():
    parser = argparse.ArgumentParser(description="Obsidian 笔记摘要批量填充")
    parser.add_argument("--dry-run", action="store_true", help="预览模式，不实际写入")
    parser.add_argument("--limit", type=int, default=0, help="限制处理数量，0 表示不限制")
    parser.add_argument("--all", action="store_true", help="处理所有缺失摘要的笔记")
    args = parser.parse_args()

    print("=" * 60)
    print("Obsidian 笔记摘要批量填充")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"模式: {'预览' if args.dry_run else '写入'}")
    print("=" * 60)

    # 找出缺少摘要的笔记
    print("\n📋 扫描缺少摘要的笔记...")
    missing_notes = find_notes_missing_summary()
    print(f"   找到 {len(missing_notes)} 个笔记缺少摘要")

    if not missing_notes:
        print("\n✅ 所有笔记都有摘要，无需处理")
        return

    # 应用限制
    if args.limit > 0:
        missing_notes = missing_notes[:args.limit]
        print(f"   限制处理: {len(missing_notes)} 个")

    print(f"\n🔄 开始处理...")
    start_time = time.time()
    success = 0
    failed = 0

    for i, note_file in enumerate(missing_notes, 1):
        print(f"\n[{i}/{len(missing_notes)}] {note_file.relative_to(VAULT_PATH)}")
        if generate_and_insert_summary(note_file, args.dry_run):
            success += 1
        else:
            failed += 1

        # 避免请求过快
        if i < len(missing_notes) and not args.dry_run:
            time.sleep(0.5)

    # 统计
    elapsed = time.time() - start_time
    print("\n" + "=" * 60)
    print("处理完成")
    print(f"  成功: {success}")
    print(f"  失败: {failed}")
    print(f"  耗时: {elapsed:.1f} 秒")
    print("=" * 60)

if __name__ == "__main__":
    main()