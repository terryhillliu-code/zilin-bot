#!/usr/bin/env python3
"""
PDF-VLM 处理管线 v1.0

智能提取 PDF 文本和图表内容，使用 VLM 描述图片/表格。

特性:
- 智能筛选：仅处理含图/表页面，节省 70% VLM 成本
- 双重表格保障：Markdown 保留数据 + VLM 描述语义
- 自适应速率控制：自动处理 API 限流

用法:
    python pdf_vlm_extract.py --dry-run                    # 预览模式
    python pdf_vlm_extract.py --limit 10                   # 只处理前 10 个
    python pdf_vlm_extract.py --all                        # 处理所有 PDF
    python pdf_vlm_extract.py --pdf /path/to/file.pdf     # 处理单个文件
"""

import os
import sys
import time
import json
import base64
import argparse
import tempfile
from pathlib import Path
from datetime import datetime
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor, as_completed

# 配置
SOURCE_BASE = Path.home() / "Documents" / "Library"
OUTPUT_BASE = Path.home() / "Documents" / "ZhiweiVault" / "Inbox" / "extracted"
MAX_FILE_SIZE = 500 * 1024 * 1024  # 500MB
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY")

# VLM 配置
VLM_MODEL = "qwen-vl-max"
VLM_API_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
VLM_MAX_TOKENS = 500
VLM_TEMPERATURE = 0.3


def find_pdf_files():
    """找出所有 PDF 文件"""
    pdf_files = []
    for root, dirs, files in os.walk(SOURCE_BASE):
        for f in files:
            if f.lower().endswith('.pdf'):
                pdf_path = Path(root) / f
                pdf_files.append(pdf_path)
    return pdf_files


def get_output_path(pdf_path: Path) -> Path:
    """
    生成输出文件路径
    规则：将相对路径的分隔符替换为下划线
    """
    try:
        rel_path = pdf_path.relative_to(SOURCE_BASE)
        # 去掉 .pdf 后缀
        name_without_ext = rel_path.with_suffix('').as_posix()
        # 替换路径分隔符为下划线
        flat_name = name_without_ext.replace('/', '_').replace('\\', '_')
        return OUTPUT_BASE / f"{flat_name}.md"
    except ValueError:
        # 不在 SOURCE_BASE 下，使用文件名
        return OUTPUT_BASE / f"{pdf_path.stem}.md"


def needs_extraction(pdf_path: Path) -> bool:
    """检查 PDF 是否需要提取"""
    output_path = get_output_path(pdf_path)
    return not output_path.exists()


# ============ PyMuPDF 文本和图片提取 ============

def extract_text_and_images(pdf_path: Path) -> tuple:
    """
    从 PDF 提取文本和图片信息

    Returns:
        (text: str, images: list, error: str)
        images: [{'page': int, 'image_data': bytes, 'bbox': tuple}, ...]
    """
    try:
        import fitz  # PyMuPDF

        text_parts = []
        images = []
        total_pages = 0

        with fitz.open(str(pdf_path)) as doc:
            total_pages = len(doc)

            # 检查是否加密
            if doc.is_encrypted:
                return None, None, "PDF 已加密，需要密码"

            for page_num, page in enumerate(doc, 1):
                # 提取文本
                page_text = page.get_text()
                if page_text.strip():
                    text_parts.append(f"--- 第 {page_num} 页 ---\n{page_text}")

                # 提取图片
                image_list = page.get_images(full=True)
                for img_index, img in enumerate(image_list):
                    xref = img[0]
                    try:
                        base_image = doc.extract_image(xref)
                        image_data = base_image["image"]

                        # 过滤小图片（面积 < 页面 1%）
                        img_rect = page.get_image_bbox(img)
                        page_rect = page.rect
                        img_area = img_rect.width * img_rect.height
                        page_area = page_rect.width * page_rect.height

                        if img_area / page_area < 0.01:
                            continue

                        images.append({
                            'page': page_num,
                            'image_data': image_data,
                            'bbox': (img_rect.x0, img_rect.y0, img_rect.x1, img_rect.y1),
                            'width': base_image.get('width', 0),
                            'height': base_image.get('height', 0)
                        })
                    except Exception:
                        continue

        if text_parts:
            full_text = "\n\n".join(text_parts)
            return full_text, images, None
        else:
            return "", images, None

    except Exception as e:
        return None, None, str(e)


# ============ Camelot 表格提取 ============

def extract_tables(pdf_path: Path) -> tuple:
    """
    从 PDF 提取表格

    Returns:
        (tables: list, error: str)
        tables: [{'page': int, 'markdown': str, 'image_path': str}, ...]
    """
    try:
        import camelot

        tables_data = []

        # 使用 lattice 模式检测有线条的表格
        try:
            tables = camelot.read_pdf(str(pdf_path), pages='all', flavor='lattice')
            for table in tables:
                page_num = table.page
                markdown = table.df.to_markdown(index=False)

                # 生成表格图片
                with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
                    table_img_path = tmp.name

                # 使用 matplotlib 生成表格图片
                import matplotlib.pyplot as plt
                fig, ax = plt.subplots(figsize=(12, len(table.df) * 0.5 + 1))
                ax.axis('off')
                ax.table(cellText=table.df.values,
                        colLabels=table.df.columns,
                        loc='center',
                        cellLoc='center')
                plt.savefig(table_img_path, bbox_inches='tight', dpi=150)
                plt.close()

                tables_data.append({
                    'page': page_num,
                    'markdown': markdown,
                    'image_path': table_img_path
                })
        except Exception as e:
            print(f"    ⚠️ Lattice 模式失败: {e}")

        # 使用 stream 模式检测无线条表格
        try:
            tables_stream = camelot.read_pdf(str(pdf_path), pages='all', flavor='stream')
            for table in tables_stream:
                page_num = table.page

                # 检查是否已存在该页的表格
                existing_pages = [t['page'] for t in tables_data]
                if page_num in existing_pages:
                    continue

                markdown = table.df.to_markdown(index=False)

                with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
                    table_img_path = tmp.name

                import matplotlib.pyplot as plt
                fig, ax = plt.subplots(figsize=(12, len(table.df) * 0.5 + 1))
                ax.axis('off')
                ax.table(cellText=table.df.values,
                        colLabels=table.df.columns,
                        loc='center',
                        cellLoc='center')
                plt.savefig(table_img_path, bbox_inches='tight', dpi=150)
                plt.close()

                tables_data.append({
                    'page': page_num,
                    'markdown': markdown,
                    'image_path': table_img_path
                })
        except Exception as e:
            print(f"    ⚠️ Stream 模式失败: {e}")

        return tables_data, None

    except ImportError:
        return None, "Camelot 未安装"
    except Exception as e:
        return None, str(e)


# ============ 页面截图 ============

def render_page_to_image(pdf_path: Path, page_num: int) -> tuple:
    """
    渲染 PDF 页面为图片

    Returns:
        (image_data: bytes, error: str)
    """
    try:
        import fitz

        with fitz.open(str(pdf_path)) as doc:
            if page_num < 1 or page_num > len(doc):
                return None, f"页码 {page_num} 超出范围"

            page = doc[page_num - 1]
            # 渲染为 PNG，150 DPI
            mat = fitz.Matrix(150 / 72, 150 / 72)
            pix = page.get_pixmap(matrix=mat)

            # 转换为 bytes
            img_data = pix.tobytes("png")
            return img_data, None

    except Exception as e:
        return None, str(e)


# ============ VLM 调用 ============

def call_vlm(image_data: bytes, prompt: str = "请详细描述这张图片中的内容，包括图表、数据和关键信息。") -> tuple:
    """
    调用 qwen-vl-max 描述图片

    Returns:
        (description: str, error: str)
    """
    import requests

    if not DASHSCOPE_API_KEY:
        return None, "DASHSCOPE_API_KEY 未配置"

    # Base64 编码图片
    image_base64 = base64.b64encode(image_data).decode('utf-8')
    image_url = f"data:image/png;base64,{image_base64}"

    headers = {
        "Authorization": f"Bearer {DASHSCOPE_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": VLM_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_url}}
                ]
            }
        ],
        "max_tokens": VLM_MAX_TOKENS,
        "temperature": VLM_TEMPERATURE
    }

    # 自适应重试
    max_retries = 5
    base_delay = 1

    for attempt in range(max_retries):
        try:
            response = requests.post(
                VLM_API_URL,
                headers=headers,
                json=payload,
                timeout=60
            )

            if response.status_code == 200:
                result = response.json()
                description = result['choices'][0]['message']['content']
                return description, None

            elif response.status_code == 429:
                # 限流，退避重试
                delay = base_delay * (2 ** attempt)
                print(f"    ⏳ VLM 限流，等待 {delay}s 后重试...")
                time.sleep(delay)
                continue

            else:
                return None, f"VLM API 错误: {response.status_code} - {response.text}"

        except requests.Timeout:
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                print(f"    ⏳ VLM 超时，等待 {delay}s 后重试...")
                time.sleep(delay)
                continue
            return None, "VLM API 超时"

        except Exception as e:
            return None, str(e)

    return None, "VLM 调用失败，超过最大重试次数"


# ============ 智能筛选 ============

def analyze_pages(pdf_path: Path) -> dict:
    """
    分析 PDF 页面，识别含图/表的页面

    Returns:
        {
            'total_pages': int,
            'text_only_pages': [int],
            'image_pages': [int],
            'table_pages': [int],
            'needs_vlm': [int]  # 需要 VLM 处理的页面
        }
    """
    import fitz

    result = {
        'total_pages': 0,
        'text_only_pages': [],
        'image_pages': [],
        'table_pages': [],
        'needs_vlm': []
    }

    try:
        with fitz.open(str(pdf_path)) as doc:
            result['total_pages'] = len(doc)

            for page_num in range(1, len(doc) + 1):
                page = doc[page_num - 1]

                # 检查图片
                has_image = False
                image_list = page.get_images(full=True)
                for img in image_list:
                    try:
                        img_rect = page.get_image_bbox(img)
                        page_rect = page.rect
                        img_area = img_rect.width * img_rect.height
                        page_area = page_rect.width * page_rect.height
                        if img_area / page_area >= 0.01:  # 面积 >= 页面 1%
                            has_image = True
                            break
                    except:
                        continue

                if has_image:
                    result['image_pages'].append(page_num)
                    result['needs_vlm'].append(page_num)
                else:
                    result['text_only_pages'].append(page_num)

        # 检查表格（需要 Camelot）
        try:
            import camelot
            tables = camelot.read_pdf(str(pdf_path), pages='all', flavor='lattice')
            for table in tables:
                if table.page not in result['table_pages']:
                    result['table_pages'].append(table.page)
                if table.page not in result['needs_vlm']:
                    result['needs_vlm'].append(table.page)
        except:
            pass

    except Exception as e:
        print(f"    ⚠️ 页面分析失败: {e}")

    return result


# ============ 主处理函数 ============

def process_pdf(pdf_path: Path, dry_run: bool = False) -> dict:
    """
    处理单个 PDF 文件

    Returns:
        {
            'path': str,
            'success': bool,
            'output': str,
            'pages': int,
            'images': int,
            'tables': int,
            'vlm_calls': int,
            'error': str
        }
    """
    result = {
        'path': str(pdf_path),
        'success': False,
        'output': None,
        'pages': 0,
        'images': 0,
        'tables': 0,
        'vlm_calls': 0,
        'error': None
    }

    temp_files = []  # 临时文件清理列表

    try:
        # 检查文件大小
        file_size = pdf_path.stat().st_size
        if file_size > MAX_FILE_SIZE:
            result['error'] = f"文件过大 ({file_size / 1024 / 1024:.1f}MB > 500MB)"
            return result

        print(f"  📄 分析页面...")

        # 分析页面
        page_info = analyze_pages(pdf_path)
        result['pages'] = page_info['total_pages']
        result['images'] = len(page_info['image_pages'])
        result['tables'] = len(page_info['table_pages'])

        print(f"     总页数: {page_info['total_pages']}")
        print(f"     含图页: {len(page_info['image_pages'])}")
        print(f"     含表页: {len(page_info['table_pages'])}")
        print(f"     需 VLM: {len(page_info['needs_vlm'])}")

        # 提取文本
        print(f"  📝 提取文本...")
        text, extracted_images, text_error = extract_text_and_images(pdf_path)
        if text_error:
            result['error'] = text_error
            return result

        # 提取表格
        print(f"  📊 检测表格...")
        tables, tables_error = extract_tables(pdf_path)
        if tables_error:
            print(f"     ⚠️ {tables_error}")
            tables = []

        # VLM 处理
        vlm_descriptions = []

        if not dry_run and page_info['needs_vlm']:
            print(f"  🤖 VLM 描述...")

            # 处理图片页面
            for page_num in page_info['image_pages']:
                print(f"     处理第 {page_num} 页...")
                img_data, img_error = render_page_to_image(pdf_path, page_num)
                if img_error:
                    print(f"     ⚠️ 渲染失败: {img_error}")
                    continue

                desc, desc_error = call_vlm(img_data, f"请详细描述这张图片中的内容。这是 PDF 第 {page_num} 页。")
                if desc_error:
                    print(f"     ⚠️ VLM 失败: {desc_error}")
                    continue

                vlm_descriptions.append({
                    'type': 'image',
                    'page': page_num,
                    'description': desc
                })
                result['vlm_calls'] += 1
                time.sleep(0.5)  # 避免请求过快

            # 处理表格
            for table in tables:
                print(f"     处理表格 (第 {table['page']} 页)...")
                try:
                    with open(table['image_path'], 'rb') as f:
                        table_img_data = f.read()
                    temp_files.append(table['image_path'])

                    desc, desc_error = call_vlm(table_img_data, "请描述这个表格的内容和关键数据。")
                    if desc_error:
                        print(f"     ⚠️ VLM 失败: {desc_error}")
                        continue

                    vlm_descriptions.append({
                        'type': 'table',
                        'page': table['page'],
                        'markdown': table['markdown'],
                        'description': desc
                    })
                    result['vlm_calls'] += 1
                    time.sleep(0.5)
                except Exception as e:
                    print(f"     ⚠️ 表格处理失败: {e}")

        # 构建输出
        output_path = get_output_path(pdf_path)

        if dry_run:
            result['success'] = True
            result['output'] = f"[预览] 将输出到 {output_path}"
            return result

        # 创建输出目录
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # 构建 Markdown 内容
        md_content = f"""---
source: {pdf_path}
extracted_at: {datetime.now().isoformat()}
pages: {result['pages']}
images: {result['images']}
tables: {result['tables']}
vlm_calls: {result['vlm_calls']}
---

## 正文内容

{text or "(无文本内容)"}

"""

        # 添加图表描述
        if vlm_descriptions:
            md_content += "## 图表描述\n\n"
            for item in vlm_descriptions:
                if item['type'] == 'image':
                    md_content += f"### 图 (第 {item['page']} 页)\n\n"
                    md_content += f"{item['description']}\n\n"
                elif item['type'] == 'table':
                    md_content += f"### 表 (第 {item['page']} 页)\n\n"
                    md_content += f"**Markdown 表格**:\n\n{item['markdown']}\n\n"
                    md_content += f"**视觉描述**: {item['description']}\n\n"

        # 写入文件
        output_path.write_text(md_content, encoding='utf-8')

        result['success'] = True
        result['output'] = str(output_path)

        return result

    except Exception as e:
        result['error'] = str(e)
        return result

    finally:
        # 清理临时文件
        for temp_file in temp_files:
            try:
                Path(temp_file).unlink(missing_ok=True)
            except:
                pass


def main():
    parser = argparse.ArgumentParser(description="PDF-VLM 处理管线")
    parser.add_argument("--dry-run", action="store_true", help="预览模式，不实际写入")
    parser.add_argument("--limit", type=int, default=0, help="限制处理数量，0 表示不限制")
    parser.add_argument("--all", action="store_true", help="处理所有 PDF")
    parser.add_argument("--pdf", type=str, help="处理单个 PDF 文件")
    parser.add_argument("--workers", type=int, default=1, help="并发数（默认 1，VLM 调用不建议高并发）")
    args = parser.parse_args()

    print("=" * 60)
    print("PDF-VLM 处理管线 v1.0")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"模式: {'预览' if args.dry_run else '写入'}")
    print(f"并发: {args.workers}")
    print("=" * 60)

    # 检查 API Key
    if not DASHSCOPE_API_KEY:
        print("❌ DASHSCOPE_API_KEY 未配置")
        print("   请在环境变量中设置: export DASHSCOPE_API_KEY=xxx")
        return

    print(f"✅ DASHSCOPE_API_KEY 已配置")

    # 检查依赖
    try:
        import fitz
        print(f"✅ PyMuPDF 已安装")
    except ImportError:
        print("❌ PyMuPDF 未安装，请运行: pip install pymupdf")
        return

    try:
        import camelot
        print(f"✅ Camelot 已安装")
    except ImportError:
        print("⚠️ Camelot 未安装，表格检测将不可用")

    # 创建输出目录
    OUTPUT_BASE.mkdir(parents=True, exist_ok=True)
    print(f"📁 输出目录: {OUTPUT_BASE}")

    # 获取 PDF 列表
    if args.pdf:
        pdf_files = [Path(args.pdf)]
    else:
        print("\n📋 扫描 PDF 文件...")
        all_pdfs = find_pdf_files()
        print(f"   找到 {len(all_pdfs)} 个 PDF 文件")

        # 过滤需要提取的
        need_extract = [p for p in all_pdfs if needs_extraction(p)]
        print(f"   需要提取: {len(need_extract)} 个")
        print(f"   已有输出: {len(all_pdfs) - len(need_extract)} 个")

        if not need_extract:
            print("\n✅ 所有 PDF 都已提取，无需处理")
            return

        pdf_files = need_extract

        # 应用限制
        if args.limit > 0:
            pdf_files = pdf_files[:args.limit]
            print(f"   限制处理: {len(pdf_files)} 个")

    # 统计
    total_size = sum(p.stat().st_size for p in pdf_files)
    print(f"   总大小: {total_size / 1024 / 1024:.1f} MB")

    print(f"\n🔄 开始处理...")
    start_time = time.time()

    success_count = 0
    fail_count = 0
    total_vlm_calls = 0
    total_images = 0
    total_tables = 0

    # 处理 PDF（串行，因为 VLM 调用有速率限制）
    for i, pdf_path in enumerate(pdf_files, 1):
        rel_path = pdf_path.relative_to(SOURCE_BASE) if pdf_path.is_relative_to(SOURCE_BASE) else pdf_path.name
        print(f"\n[{i}/{len(pdf_files)}] {rel_path}")

        result = process_pdf(pdf_path, args.dry_run)

        if result['success']:
            success_count += 1
            total_vlm_calls += result['vlm_calls']
            total_images += result['images']
            total_tables += result['tables']
            print(f"  ✅ 完成: {result['pages']} 页, {result['images']} 图, {result['tables']} 表, {result['vlm_calls']} VLM 调用")
        else:
            fail_count += 1
            print(f"  ❌ 失败: {result['error']}")

    # 统计
    elapsed = time.time() - start_time
    print("\n" + "=" * 60)
    print("处理完成")
    print(f"  成功: {success_count}")
    print(f"  失败: {fail_count}")
    print(f"  总图片: {total_images}")
    print(f"  总表格: {total_tables}")
    print(f"  VLM 调用: {total_vlm_calls}")
    print(f"  耗时: {elapsed:.1f} 秒")
    if total_vlm_calls > 0:
        print(f"  平均 VLM 调用: {total_vlm_calls / success_count:.1f} 次/文件")
    print("=" * 60)


if __name__ == "__main__":
    main()