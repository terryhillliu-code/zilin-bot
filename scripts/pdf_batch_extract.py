#!/usr/bin/env python3
"""
PDF 批量提取工具

将 PDF 文件提取为 .md 文本文件，用于：
1. Obsidian 笔记摘要生成
2. 向量化入库
3. Obsidian 直接查看

用法:
    python pdf_batch_extract.py --dry-run    # 预览模式
    python pdf_batch_extract.py --limit 10   # 只处理前 10 个
    python pdf_batch_extract.py --all        # 处理所有 PDF
"""

import os
import sys
import time
import argparse
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# 配置
SOURCE_BASE = Path.home() / "Documents" / "Library"
MAX_FILE_SIZE = 500 * 1024 * 1024  # 500MB，超过此大小的跳过
TIMEOUT_SECONDS = 60  # 单个 PDF 提取超时


def find_pdf_files():
    """找出所有 PDF 文件"""
    pdf_files = []
    for root, dirs, files in os.walk(SOURCE_BASE):
        for f in files:
            if f.lower().endswith('.pdf'):
                pdf_path = Path(root) / f
                pdf_files.append(pdf_path)
    return pdf_files


def needs_extraction(pdf_path: Path) -> bool:
    """检查 PDF 是否需要提取（对应的 .md 不存在）"""
    md_path = pdf_path.with_suffix('.md')
    return not md_path.exists()


def extract_pdf_to_text(pdf_path: Path) -> tuple:
    """
    从 PDF 提取文本

    Returns:
        (success: bool, text: str, error: str, is_scanned: bool)
    """
    try:
        import fitz  # PyMuPDF

        text_parts = []
        total_pages = 0
        empty_pages = 0

        with fitz.open(str(pdf_path)) as doc:
            total_pages = len(doc)

            # 检查是否加密
            if doc.is_encrypted:
                return False, None, "PDF 已加密，需要密码", False

            for page_num, page in enumerate(doc, 1):
                page_text = page.get_text()
                if page_text.strip():
                    text_parts.append(f"--- 第 {page_num} 页 ---\n{page_text}")
                else:
                    empty_pages += 1

        # 检测扫描版 PDF（超过 80% 页面无文字）
        if total_pages > 0 and empty_pages / total_pages > 0.8:
            return False, None, f"疑似扫描版 PDF（{empty_pages}/{total_pages} 页无文字），需要 OCR", True

        if text_parts:
            full_text = "\n\n".join(text_parts)
            return True, full_text, None, False
        else:
            return False, None, "PDF 中未提取到有效文字", True

    except Exception as e:
        return False, None, str(e), False


def process_pdf(pdf_path: Path, dry_run: bool = False) -> dict:
    """
    处理单个 PDF 文件

    Returns:
        {
            'path': str,
            'success': bool,
            'output': str,
            'size': int,
            'error': str
        }
    """
    result = {
        'path': str(pdf_path),
        'success': False,
        'output': None,
        'size': 0,
        'error': None
    }

    try:
        # 检查文件大小
        file_size = pdf_path.stat().st_size
        result['size'] = file_size

        if file_size > MAX_FILE_SIZE:
            result['error'] = f"文件过大 ({file_size / 1024 / 1024:.1f}MB > 500MB)，跳过"
            return result

        # 提取文本
        success, text, error = extract_pdf_to_text(pdf_path)

        if not success:
            result['error'] = error or "提取失败"
            return result

        if dry_run:
            result['success'] = True
            result['output'] = f"[预览] 将保存 {len(text)} 字符"
            return result

        # 保存为 .md
        md_path = pdf_path.with_suffix('.md')
        md_path.write_text(text, encoding='utf-8')

        result['success'] = True
        result['output'] = str(md_path)
        result['size'] = len(text)

        return result

    except Exception as e:
        result['error'] = str(e)
        return result


def main():
    parser = argparse.ArgumentParser(description="PDF 批量提取为 .md")
    parser.add_argument("--dry-run", action="store_true", help="预览模式，不实际写入")
    parser.add_argument("--limit", type=int, default=0, help="限制处理数量，0 表示不限制")
    parser.add_argument("--all", action="store_true", help="处理所有 PDF")
    parser.add_argument("--workers", type=int, default=4, help="并发数（默认 4）")
    args = parser.parse_args()

    print("=" * 60)
    print("PDF 批量提取工具")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"模式: {'预览' if args.dry_run else '写入'}")
    print(f"并发: {args.workers}")
    print("=" * 60)

    # 检查 PyMuPDF
    try:
        import fitz
        print(f"✅ PyMuPDF 已安装")
    except ImportError:
        print("❌ PyMuPDF 未安装，请运行: pip install pymupdf")
        return

    # 扫描 PDF 文件
    print("\n📋 扫描 PDF 文件...")
    all_pdfs = find_pdf_files()
    print(f"   找到 {len(all_pdfs)} 个 PDF 文件")

    # 过滤需要提取的
    need_extract = [p for p in all_pdfs if needs_extraction(p)]
    print(f"   需要提取: {len(need_extract)} 个")
    print(f"   已有 .md: {len(all_pdfs) - len(need_extract)} 个")

    if not need_extract:
        print("\n✅ 所有 PDF 都已提取，无需处理")
        return

    # 应用限制
    if args.limit > 0:
        need_extract = need_extract[:args.limit]
        print(f"   限制处理: {len(need_extract)} 个")

    # 统计文件大小
    total_size = sum(p.stat().st_size for p in need_extract)
    print(f"   总大小: {total_size / 1024 / 1024:.1f} MB")

    print(f"\n🔄 开始处理...")
    start_time = time.time()

    success_count = 0
    fail_count = 0
    skip_count = 0
    total_chars = 0

    # 并发处理
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(process_pdf, p, args.dry_run): p for p in need_extract}

        for i, future in enumerate(as_completed(futures), 1):
            pdf_path = futures[future]
            result = future.result()

            # 显示进度
            rel_path = pdf_path.relative_to(SOURCE_BASE)
            if result['success']:
                success_count += 1
                total_chars += result.get('size', 0)
                print(f"[{i}/{len(need_extract)}] ✅ {rel_path.name[:40]}: {result.get('size', 0)} 字符")
            else:
                fail_count += 1
                print(f"[{i}/{len(need_extract)}] ❌ {rel_path.name[:40]}: {result.get('error', '未知错误')}")

    # 统计
    elapsed = time.time() - start_time
    print("\n" + "=" * 60)
    print("处理完成")
    print(f"  成功: {success_count}")
    print(f"  失败: {fail_count}")
    print(f"  总字符: {total_chars:,}")
    print(f"  耗时: {elapsed:.1f} 秒")
    if success_count > 0:
        print(f"  平均速度: {total_chars // success_count:,} 字符/文件")
    print("=" * 60)


if __name__ == "__main__":
    main()