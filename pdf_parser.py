"""
PDF 解析模块
独立的 PDF 文件解析器，支持文字提取、表格提取和内容分析
"""

import os
import re
import tempfile
import subprocess
import base64
from typing import Optional, List, Tuple

# 导入依赖（由 ws_client.py 初始化）
client = None
reply_message = None
TaskLogger = None
time = None


def init_pdf_parser(global_client, global_reply_message, global_task_logger, global_time):
    """初始化 PDF 解析模块的全局依赖"""
    global client, reply_message, TaskLogger, time
    client = global_client
    reply_message = global_reply_message
    TaskLogger = global_task_logger
    time = global_time


# ========== PDF 文件处理 ==========

def download_pdf(message_id: str, file_key: str) -> str:
    """下载飞书 PDF 文件"""
    try:
        from lark_oapi.api.im.v1 import GetMessageResourceRequest
        tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        tmp_path = tmp_file.name
        tmp_file.close()

        request = GetMessageResourceRequest.builder() \
            .message_id(message_id) \
            .file_key(file_key) \
            .type("file") \
            .build()
        response = client.im.v1.message_resource.get(request)

        if response.success():
            with open(tmp_path, "wb") as f:
                f.write(response.file.read())
            size = os.path.getsize(tmp_path)
            print(f"✅ PDF 下载成功: {tmp_path} ({size} bytes)")
            return tmp_path
        else:
            print(f"❌ PDF 下载失败: {response.code} - {response.msg}")
            return None
    except Exception as e:
        print(f"❌ PDF 下载异常: {e}")
        return None


def extract_pdf_text(pdf_path: str) -> Tuple[Optional[str], Optional[str]]:
    """
    从 PDF 中提取文字内容
    返回: (提取的文字, 错误信息)
    """
    try:
        if not os.path.exists(pdf_path):
            return None, "PDF 文件不存在"

        # 使用 PyMuPDF (fitz) 提取文字
        try:
            import fitz  # PyMuPDF
            print(f"📄 使用 PyMuPDF 提取文字: {pdf_path}")

            text_parts = []
            with fitz.open(pdf_path) as doc:
                for page_num, page in enumerate(doc, 1):
                    page_text = page.get_text()
                    if page_text.strip():
                        text_parts.append(f"--- 第 {page_num} 页 ---\n{page_text}")

            if text_parts:
                full_text = "\n\n".join(text_parts)
                print(f"✅ PyMuPDF 提取完成: {len(full_text)} 字符")
                return full_text, None
            else:
                return None, "PDF 中未提取到有效文字"

        except ImportError:
            # PyMuPDF 不可用，尝试 pdfplumber
            try:
                import pdfplumber
                print(f"📄 使用 pdfplumber 提取文字: {pdf_path}")

                text_parts = []
                with pdfplumber.open(pdf_path) as pdf:
                    for page_num, page in enumerate(pdf.pages, 1):
                        page_text = page.extract_text()
                        if page_text:
                            text_parts.append(f"--- 第 {page_num} 页 ---\n{page_text}")

                if text_parts:
                    full_text = "\n\n".join(text_parts)
                    print(f"✅ pdfplumber 提取完成: {len(full_text)} 字符")
                    return full_text, None
                else:
                    return None, "PDF 中未提取到有效文字"

            except ImportError:
                return None, "PDF 解析库不可用，请安装 PyMuPDF 或 pdfplumber"

    except Exception as e:
        print(f"❌ PDF 提取异常: {e}")
        return None, f"提取失败: {str(e)}"


def extract_pdf_summary(pdf_path: str, max_pages: int = 10) -> str:
    """
    生成 PDF 摘要（前 max_pages 页）
    返回摘要字符串
    """
    try:
        text, error = extract_pdf_text(pdf_path)
        if error:
            return f"❌ 提取失败: {error}"

        if not text:
            return "❌ 未能提取到有效内容"

        # 长度限制
        if len(text) > 15000:
            text = text[:15000] + "\n\n...(内容过长已截断)"

        return text

    except Exception as e:
        return f"❌ PDF 处理异常: {str(e)}"


def handle_pdf_async(message_id: str, file_key: str, user_id: str):
    """异步处理 PDF 文件"""
    def _process():
        try:
            pdf_path = download_pdf(message_id, file_key)
            if not pdf_path:
                reply_message(message_id, "❌ PDF 文件下载失败，请重试")
                return

            reply_message(message_id, "📄 正在提取 PDF 内容...\n\n⏳ 请稍候")

            summary = extract_pdf_summary(pdf_path)

            # 清理临时文件
            if os.path.exists(pdf_path):
                os.remove(pdf_path)

            # 分段回复（飞书限制）
            if len(summary) > 3500:
                # 发送完整摘要到日志或文件
                reply_message(message_id, f"📄 PDF 提取完成 (共 {len(summary)} 字符)")
                reply_message(message_id, summary[:3500] + "\n\n...(后续内容已发送至日志)")
                print(f"PDF 完整内容:\n{summary}")
            else:
                reply_message(message_id, f"📄 **PDF 内容提取**\n\n{summary}")

            TaskLogger.log_task("PDF 处理", "完成", f"{message_id}.pdf")

        except Exception as e:
            print(f"❌ PDF 处理异常: {e}")
            import traceback
            traceback.print_exc()
            reply_message(message_id, f"❌ PDF 处理异常: {str(e)}")

    import threading
    thread = threading.Thread(target=_process, daemon=True)
    thread.start()


def parse_pdf_from_path(file_path: str) -> str:
    """
    直接从本地路径解析 PDF（不通过飞书下载）
    用于处理已上传到本地的 PDF 文件
    """
    if not os.path.exists(file_path):
        return "❌ 文件不存在"

    return extract_pdf_summary(file_path)


# ========== PDF 表格提取 ==========

def extract_pdf_tables(pdf_path: str, pages: Optional[List[int]] = None) -> List[Tuple[int, List[List[str]]]]:
    """
    从 PDF 中提取表格

    Args:
        pdf_path: PDF 文件路径
        pages: 要处理的页码列表，None 表示所有页

    Returns:
        List[Tuple[int, List[List[str]]]]: [(页码, 表格数据), ...]
    """
    try:
        import fitz  # PyMuPDF

        doc = fitz.open(pdf_path)

        if pages is None:
            pages = list(range(len(doc)))

        results = []

        for page_num in pages:
            if page_num >= len(doc):
                continue

            page = doc[page_num]
            tables = page.find_tables()

            if tables.tables:
                table_data = []
                for table in tables.tables:
                    data = table.extract()
                    if data:
                        table_data.extend(data)

                if table_data:
                    results.append((page_num + 1, table_data))
                    print(f"PDF 表格提取: 第 {page_num + 1} 页 | {len(table_data)} 行")

        doc.close()
        return results

    except ImportError:
        print("❌ 未安装 PyMuPDF，请运行: pip install PyMuPDF")
        return []
    except Exception as e:
        print(f"PDF 表格提取失败: {e}")
        return []


def tables_to_markdown(table_data: List[List[str]]) -> str:
    """
    将表格数据转换为 Markdown 格式

    Args:
        table_data: 表格数据，二维列表

    Returns:
        str: Markdown 格式的表格
    """
    if not table_data:
        return ""

    num_cols = max(len(row) for row in table_data)

    lines = []

    if table_data:
        header = table_data[0]
        row_str = "| " + " | ".join(str(h).strip() for h in header) + " |"
        lines.append(row_str)

        separator = "|" + "|".join("---" for _ in range(num_cols)) + "|"
        lines.append(separator)

        for row in table_data[1:]:
            row_str = "| " + " | ".join(str(cell).strip() for cell in row) + " |"
            lines.append(row_str)

    return "\n".join(lines)


def extract_tables_summary(pdf_path: str) -> str:
    """
    提取 PDF 中所有表格并转换为 Markdown

    Args:
        pdf_path: PDF 文件路径

    Returns:
        str: Markdown 格式的表格摘要
    """
    tables = extract_pdf_tables(pdf_path)

    if not tables:
        return "PDF 中未提取到表格"

    summary_parts = []
    for page_num, data in tables:
        md = tables_to_markdown(data)
        summary_parts.append(f"### 第 {page_num} 页表格\n\n{md}")

    return "\n\n".join(summary_parts)


# ========== PDF base64 处理 ==========

def extract_text_from_pdf_base64(pdf_base64: str) -> Tuple[Optional[str], int]:
    """
    从 base64 编码的 PDF 数据中提取文本

    Args:
        pdf_base64: PDF 文件的 base64 编码字符串

    Returns:
        Tuple[str, int]: (提取的文本内容, 页数)
    """
    try:
        import fitz

        pdf_bytes = base64.b64decode(pdf_base64)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_file:
            tmp_file.write(pdf_bytes)
            tmp_path = tmp_file.name

        try:
            doc = fitz.open(tmp_path)
            total_pages = len(doc)
            text_parts = []

            for page_num in range(total_pages):
                page = doc[page_num]
                text = page.get_text()
                if text.strip():
                    text_parts.append(f"--- 第 {page_num + 1} 页 ---\n{text}")

            doc.close()

            full_text = "\n\n".join(text_parts)
            full_text = re.sub(r'\n{3,}', '\n\n', full_text)

            print(f"PDF base64 提取成功: {total_pages} 页 | {len(full_text)} 字符")
            return full_text, total_pages

        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    except ImportError:
        print("❌ 未安装 PyMuPDF，请运行: pip install PyMuPDF")
        return None, 0
    except Exception as e:
        print(f"PDF base64 提取失败: {e}")
        return None, 0


def extract_tables_from_pdf_base64(pdf_base64: str) -> List[Tuple[int, List[List[str]]]]:
    """
    从 base64 编码的 PDF 数据中提取表格

    Args:
        pdf_base64: PDF 文件的 base64 编码字符串

    Returns:
        List[Tuple[int, List[List[str]]]]: [(页码, 表格数据), ...]
    """
    try:
        import fitz

        pdf_bytes = base64.b64decode(pdf_base64)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_file:
            tmp_file.write(pdf_bytes)
            tmp_path = tmp_file.name

        try:
            doc = fitz.open(tmp_path)
            results = []

            for page_num in range(len(doc)):
                page = doc[page_num]
                tables = page.find_tables()

                if tables.tables:
                    table_data = []
                    for table in tables.tables:
                        data = table.extract()
                        if data:
                            table_data.extend(data)

                    if table_data:
                        results.append((page_num + 1, table_data))
                        print(f"PDF base64 表格提取: 第 {page_num + 1} 页 | {len(table_data)} 行")

            doc.close()
            return results

        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    except ImportError:
        print("❌ 未安装 PyMuPDF，请运行: pip install PyMuPDF")
        return []
    except Exception as e:
        print(f"PDF base64 表格提取失败: {e}")
        return []


# ========== PDF 内容分析 ==========

def analyze_pdf_content(text: str, pages: int, question: str = None) -> str:
    """
    使用 AI 分析 PDF 内容

    Args:
        text: PDF 提取的文本
        pages: 页数
        question: 用户的特定问题，None 表示生成 AI 硬件架构师专属摘要

    Returns:
        str: AI 分析结果
    """
    try:
        import httpx

        # 导入专属摘要模块
        from scripts.obsidian_summary_filler import SUMMARY_PROMPT, get_text_for_summary, _get_api_key

        api_key = _get_api_key()
        if not api_key:
            return "❌ 系统配置异常，请联系管理员"

        # 使用智能截断策略
        prompt_text = get_text_for_summary(text)

        if question:
            # 有特定问题时，使用问答模式
            prompt = f"""请分析以下 PDF 文档内容，回答用户的问题：

文档信息: {pages} 页

文档内容:
{prompt_text[:8000]}

用户问题:
{question}

请结合文档内容，准确回答用户的问题。"""
        else:
            # 无特定问题时，使用 AI 硬件架构师专属摘要 prompt
            prompt = SUMMARY_PROMPT + f"\n\n## 文档信息\n页数: {pages} 页\n字符数: {len(prompt_text)}\n\n## 文档内容\n{prompt_text}"

        print(f"PDF 分析: 调用 qwen3.5-plus...")
        response = httpx.post(
            "https://coding.dashscope.aliyuncs.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            },
            json={
                "model": "qwen3.5-plus",
                "messages": [{
                    "role": "user",
                    "content": prompt
                }],
                "max_tokens": 1500
            },
            timeout=90
        )

        if response.status_code == 200:
            result = response.json()["choices"][0]["message"]["content"]
            print(f"PDF 分析完成: {len(result)} 字符")
            return f"📄 **PDF 文档分析**\n\n{result}"
        else:
            print(f"PDF 分析失败: {response.status_code}")
            return f"❌ PDF 分析失败: {response.status_code}"

    except Exception as e:
        print(f"PDF 分析异常: {e}")
        return f"❌ PDF 分析异常: {str(e)}"


def process_pdf_message(message_id: str, file_key: str, user_id: str):
    """
    处理用户发送的 PDF 文件消息

    Args:
        message_id: 消息 ID
        file_key: 文件 key
        user_id: 用户 ID
    """
    try:
        # 下载 PDF
        pdf_path = download_pdf(message_id, file_key)
        if not pdf_path:
            reply_message(message_id, "❌ PDF 下载失败，请重试")
            return

        # 提取文本
        text, pages = extract_pdf_text(pdf_path)
        if not text:
            reply_message(message_id, "❌ PDF 内容提取失败")
            if os.path.exists(pdf_path):
                os.remove(pdf_path)
            return

        # 清理临时文件
        if os.path.exists(pdf_path):
            os.remove(pdf_path)

        # 检查内容长度
        if len(text) > 3500:
            preview = text[:3500]
            reply_message(message_id, f"PDF 提取成功: {pages} 页 | {len(text)} 字符\n\n{preview}...\n\n💡 内容过长，建议使用 /pdf 命令获取完整内容")
        else:
            reply_message(message_id, f"PDF 提取成功: {pages} 页\n\n{text}")

        TaskLogger.log_task("PDF 处理", "完成", f"{pages} 页")

    except Exception as e:
        print(f"PDF 处理异常: {e}")
        reply_message(message_id, f"❌ PDF 处理异常: {str(e)}")
