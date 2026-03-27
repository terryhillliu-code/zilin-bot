#!/usr/bin/env python3
"""
PDF-VLM 处理管线 v2.0 ⭐

智能提取 PDF 文本和图表内容，使用 VLM 描述图片/表格。

v2.0 更新 (2026-03-16):
- 放弃 Camelot 表格提取（准确率 0%），改用纯 VLM 方案
- 结构化 JSON 输出：表格数据直接提取为 Markdown 表格
- 智能类型识别：table/chart/figure/text

特性:
- 智能筛选：仅处理含图页面，节省 70% VLM 成本
- 结构化输出：JSON → Markdown 表格转换
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
VLM_MAX_TOKENS = 2000  # 增加以支持表格数据提取 ⭐ v2.0
VLM_TEMPERATURE = 0.3

# 结构化数据提取 Prompt ⭐ v2.0
VLM_PROMPT = """分析此页面内容，按以下 JSON 格式输出：

```json
{
  "type": "table|chart|figure|text",
  "title": "图表标题（如果有）",
  "data": {
    "headers": ["列1", "列2", ...],
    "rows": [["值1", "值2", ...], ...]
  },
  "key_insights": ["关键发现1", "关键发现2", ...],
  "description": "图表/图片的文字描述"
}
```

规则：
- 如果是表格：完整提取所有行列数据到 data.rows
- 如果是图表：提取关键数据点，在 description 中描述趋势
- 如果是流程图/示意图：用 key_insights 描述流程步骤
- 如果只是文字：type 设为 "text"，在 description 中总结内容"""


# 智能预检配置 (Smart Filter) ⭐ v3.0
SMART_FILTER_MODEL = "qwen3.5-plus"
SMART_FILTER_PROMPT = """你是一个资深的硬件系统架构师。请评估以下PDF文档前几页的内容，判断它是否是一份包含高价值图表、架构分析或对比数据的【深度硬件/系统技术研报】。

高价值主题特征：服务器处理器、GPU架构演进、芯片互联(CXL/NVLink)、网络通信(RDMA)、数据中心基础设施(液冷/供电)、硬件TCO分析等。
低价值主题特征：纯深度学习算法原理、纯数学公式推导、内容空泛的宏观新闻、纯软件工程（如前端开发）。

请在分析后，在最后一行输出一个 0 到 100 的整数评分。
评分标准：
- 90-100分：极高价值的硬件架构白皮书、厂商Specs、券商深度硬核研报（必须做图表提取）。
- 70-89分：包含具体硬件对比数据、性能压测分析的有效报告。
- <70分：偏软件算法、宏观简报，或不需要提取图表的普通文档。

请在最后一行强制输出且仅输出两部分内容：[分析结论] 和 [评分: xx]。例如：
分析结论：本文档详细分析了HBM的带宽瓶颈...
评分: 85"""

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


    return None, "VLM 调用失败，超过最大重试次数"


# ============ 智能预检 (Smart Filter) ⭐ v3.0 ============

def preflight_scan(text: str) -> tuple:
    """
    预检文本前几页，判断是否值得发起 VLM
    Returns: (score: int, reason: str)
    """
    import requests
    import re

    if not DASHSCOPE_API_KEY:
        return 100, "未配置 API Key，跳过预检"

    # 截取前 4000 字符（大约前 5-10 页内容）
    preview_text = text[:4000]

    headers = {
        "Authorization": f"Bearer {DASHSCOPE_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": SMART_FILTER_MODEL,
        "messages": [
            {"role": "system", "content": SMART_FILTER_PROMPT},
            {"role": "user", "content": f"以下是研报前几页的文本提取内容：\n\n{preview_text}"}
        ],
        "temperature": 0.1
    }

    try:
        response = requests.post(VLM_API_URL, headers=headers, json=payload, timeout=30)
        if response.status_code == 200:
            content = response.json()['choices'][0]['message']['content']
            
            # 正则提取评分
            score_match = re.search(r'评分:\s*(\d+)', content)
            if score_match:
                score = int(score_match.group(1))
                reason = content.replace(f"评分: {score}", "").strip()
                return score, reason
            else:
                return 100, "预检解析失败，默认放行"
    except Exception as e:
        return 100, f"预检请求异常，默认放行: {e}"

    return 100, "预检响应异常，默认放行"

# ============ 结构化输出解析 ⭐ v2.0 ============

def parse_vlm_response(response: str) -> dict:
    """
    解析 VLM 响应为结构化数据

    Returns:
        dict 或 None
    """
    import re
    try:
        # 尝试提取 JSON 块
        json_match = re.search(r'```json\s*(\{[\s\S]*?\})\s*```', response)
        if json_match:
            return json.loads(json_match.group(1))
        # 尝试直接解析
        return json.loads(response)
    except:
        return None


def format_vlm_output(vlm_data: dict, page_num: int) -> str:
    """
    格式化 VLM 输出为 Markdown

    Returns:
        Markdown 格式的字符串
    """
    content_type = vlm_data.get('type', 'unknown')
    title = vlm_data.get('title', '')
    data = vlm_data.get('data', {})
    insights = vlm_data.get('key_insights', [])
    description = vlm_data.get('description', '')

    output = f"### 第 {page_num} 页\n\n"

    # 类型标签
    type_labels = {
        'table': '📊 表格',
        'chart': '📈 图表',
        'figure': '🖼️ 图片',
        'text': '📝 内容'
    }
    output += f"**类型**: {type_labels.get(content_type, content_type)}\n\n"

    if title:
        output += f"**标题**: {title}\n\n"

    # 表格数据
    if content_type == 'table' and data.get('headers') and data.get('rows'):
        headers = data['headers']
        rows = data['rows']
        # 生成 Markdown 表格
        output += "| " + " | ".join(str(h) for h in headers) + " |\n"
        output += "|" + "|".join(["---"] * len(headers)) + "|\n"
        for row in rows[:10]:  # 最多显示 10 行
            output += "| " + " | ".join(str(cell) for cell in row) + " |\n"
        if len(rows) > 10:
            output += f"*... 共 {len(rows)} 行*\n"
        output += "\n"

    # 关键洞察
    if insights:
        output += "**关键信息**:\n"
        for insight in insights[:5]:
            output += f"- {insight}\n"
        output += "\n"

    # 描述
    if description:
        output += f"**描述**: {description}\n\n"

    return output


# ============ 智能筛选 ============

def analyze_pages(pdf_path: Path) -> dict:
    """
    分析 PDF 页面，识别含图的页面

    Returns:
        {
            'total_pages': int,
            'text_only_pages': [int],
            'image_pages': [int],
            'needs_vlm': [int]  # 需要 VLM 处理的页面
        }
    """
    import fitz

    result = {
        'total_pages': 0,
        'text_only_pages': [],
        'image_pages': [],
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
                        xref = img[0]
                        base_image = doc.extract_image(xref)
                        img_area = base_image["width"] * base_image["height"]
                        page_area = page.rect.width * page.rect.height
                        # 图片面积 >= 页面 1% 且大小 > 1KB
                        if img_area / page_area >= 0.01 and len(base_image["image"]) > 1000:
                            has_image = True
                            break
                    except:
                        continue

                if has_image:
                    result['image_pages'].append(page_num)
                    result['needs_vlm'].append(page_num)
                else:
                    result['text_only_pages'].append(page_num)

    except Exception as e:
        print(f"    ⚠️ 页面分析失败: {e}")

    return result


# ============ 主处理函数 ============

def process_pdf(pdf_path: Path, dry_run: bool = False, smart_filter: bool = False) -> dict:
    """
    处理单个 PDF 文件

    Returns:
        {
            'path': str,
            'success': bool,
            'output': str,
            'pages': int,
            'images': int,
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
        'vlm_calls': 0,
        'error': None,
        'smart_score': None
    }

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

        print(f"     总页数: {page_info['total_pages']}")
        print(f"     含图页: {len(page_info['image_pages'])}")

        # 提取文本
        print(f"  📝 提取文本...")
        text, extracted_images, text_error = extract_text_and_images(pdf_path)
        if text_error:
            result['error'] = text_error
            return result

        # 智能预检 (Smart Filter)
        skip_vlm = False
        if smart_filter and text:
            print(f"  🧠 智能预检 (Smart Filter)...")
            score, reason = preflight_scan(text)
            result['smart_score'] = score
            print(f"     预检得分: {score}/100")
            if score < 70:
                print(f"     ⏭️ 判定为非高价值硬件研报，跳过 VLM 识别。\n     原因: {reason[:100]}...")
                skip_vlm = True
        
        # VLM 处理 ⭐ v2.0 纯 VLM 方案
        vlm_outputs = []

        if not dry_run and page_info['image_pages'] and not skip_vlm:
            print(f"  🤖 VLM 描述 ({len(page_info['image_pages'])} 页)...")

            for page_num in page_info['image_pages']:
                print(f"     处理第 {page_num} 页...")
                img_data, img_error = render_page_to_image(pdf_path, page_num)
                if img_error:
                    print(f"     ⚠️ 渲染失败: {img_error}")
                    continue

                # 使用结构化 Prompt
                desc, desc_error = call_vlm(img_data, VLM_PROMPT)
                if desc_error:
                    print(f"     ⚠️ VLM 失败: {desc_error}")
                    continue

                # 解析结构化输出
                vlm_data = parse_vlm_response(desc)
                if vlm_data:
                    formatted = format_vlm_output(vlm_data, page_num)
                    vlm_outputs.append({
                        'page': page_num,
                        'formatted': formatted,
                        'type': vlm_data.get('type', 'unknown')
                    })
                    print(f"     ✅ 完成 ({vlm_data.get('type', 'unknown')})")
                else:
                    # 解析失败，使用原始输出
                    vlm_outputs.append({
                        'page': page_num,
                        'formatted': f"### 第 {page_num} 页\n\n{desc}\n\n",
                        'type': 'raw'
                    })
                    print(f"     ✅ 完成 (原始输出)")

                result['vlm_calls'] += 1
                time.sleep(0.5)  # 避免请求过快

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
vlm_calls: {result['vlm_calls']}
smart_score: {result.get('smart_score', 'N/A')}
---

## 正文内容

{text or "(无文本内容)"}

"""

        # 添加图表描述
        if vlm_outputs:
            md_content += "## 图表描述\n\n"
            for item in vlm_outputs:
                md_content += item['formatted']
        elif skip_vlm:
            md_content += f"## 图表描述\n\n本文档经 Smart Filter 预检得分为 {result.get('smart_score')} (阈值 70)，判定为非高价值硬件研报，自动跳过 VLM 昂贵的图表提取过程。\n"
        elif page_info['image_pages']:
            md_content += "## 图表描述\n\n本文档含图页面 VLM 处理失败。\n"
        else:
            md_content += "## 图表描述\n\n本文档无显著图片内容。\n"

        # 写入文件
        output_path.write_text(md_content, encoding='utf-8')

        result['success'] = True
        result['output'] = str(output_path)

        return result

    except Exception as e:
        result['error'] = str(e)
        return result


def main():
    parser = argparse.ArgumentParser(description="PDF-VLM 处理管线")
    parser.add_argument("--dry-run", action="store_true", help="预览模式，不实际写入")
    parser.add_argument("--limit", type=int, default=0, help="限制处理数量，0 表示不限制")
    parser.add_argument("--all", action="store_true", help="处理所有 PDF")
    parser.add_argument("--pdf", type=str, help="处理单个 PDF 文件")
    parser.add_argument("--workers", type=int, default=1, help="并发数（默认 1，VLM 调用不建议高并发）")
    parser.add_argument("--smart-filter", action="store_true", help="开启智能过滤，由极廉价模型预读判定为高价值研报后才发起 VLM（适合盲扫大批量 PDF）")
    args = parser.parse_args()

    print("=" * 60)
    print("PDF-VLM 处理管线 v3.0 (Smart Filter 增强)")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"模式: {'预览' if args.dry_run else '写入'}")
    print(f"智能预检: {'开启' if args.smart_filter else '关闭'}")
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

    # 处理 PDF（串行，因为 VLM 调用有速率限制）
    for i, pdf_path in enumerate(pdf_files, 1):
        rel_path = pdf_path.relative_to(SOURCE_BASE) if pdf_path.is_relative_to(SOURCE_BASE) else pdf_path.name
        print(f"\n[{i}/{len(pdf_files)}] {rel_path}")

        result = process_pdf(pdf_path, args.dry_run, args.smart_filter)

        if result['success']:
            success_count += 1
            total_vlm_calls += result['vlm_calls']
            total_images += result['images']
            print(f"  ✅ 完成: {result['pages']} 页, {result['images']} 图, {result['vlm_calls']} VLM 调用")
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
    print(f"  VLM 调用: {total_vlm_calls}")
    print(f"  耗时: {elapsed:.1f} 秒")
    if total_vlm_calls > 0:
        print(f"  平均 VLM 调用: {total_vlm_calls / success_count:.1f} 次/文件")
    print("=" * 60)


if __name__ == "__main__":
    main()