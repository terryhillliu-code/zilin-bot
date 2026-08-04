#!/usr/bin/env python3
"""PDF 知识蒸馏器（2026-08-02）

把 PDF 文档蒸馏成结构化 Obsidian 笔记，与视频笔记完全同模板。
链路：rag venv 提取文本(pymupdf) → 复用 douyin_distiller 的
KnowledgeDistiller(两阶段 LLM 蒸馏) + MarkdownWriter(笔记渲染)
→ RAG 增量入库。由 media_handler.process_pdf 以子进程调用。

已知限制（与长视频一致）：LLM 深度分析聚焦前 ~8000 字符，
其余部分只做分段要点提取；全文会写入笔记 <details> 并随 RAG
入库，后续可用 /ask 对全文提问。

用法: python3 pdf_distiller.py --pdf <file.pdf> --output-dir <dir>
成功输出 "✅ Done! Output: <md路径>"（与视频蒸馏器协议一致）；
失败向 stderr 输出 {"error_type": ..., "error_message": ...} JSON。
"""
import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # scripts/ 目录

from douyin_distiller import (  # noqa: E402
    AppConfig, VideoInfo, TranscriptResult,
    KnowledgeDistiller, MarkdownWriter, _trigger_rag_ingest,
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("pdf_distiller")

RAG_PYTHON = Path.home() / "zhiwei-rag" / "venv" / "bin" / "python"
EXTRACT_SCRIPT = Path(__file__).resolve().parent / "pdf_extract.py"
MAX_CHARS = 60000  # 超长 PDF 截断保护
MIN_CHARS = 200    # 低于此长度视为扫描件/图片型 PDF


def _fail(error_type: str, message: str) -> int:
    print(json.dumps({"error_type": error_type, "error_message": message},
                     ensure_ascii=False), file=sys.stderr)
    return 1


def extract_text(pdf_path: Path) -> dict:
    """用 rag venv 的 pymupdf 提取全文（shared-venv 无此依赖）"""
    result = subprocess.run(
        [str(RAG_PYTHON), str(EXTRACT_SCRIPT), str(pdf_path)],
        capture_output=True, text=True, timeout=180)
    if result.returncode != 0:
        raise RuntimeError(f"文本提取子进程失败: {result.stderr[-300:]}")
    return json.loads(result.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description="PDF 知识蒸馏器")
    parser.add_argument("--pdf", required=True, help="PDF 文件路径")
    parser.add_argument("--output-dir", required=True, help="笔记输出目录")
    args = parser.parse_args()

    pdf_path = Path(args.pdf).expanduser()
    if not pdf_path.exists() or pdf_path.suffix.lower() != ".pdf":
        return _fail("module_error", "文件不存在或不是 PDF")

    # 1. 提取文本
    logger.info(f"Step 1: 提取 PDF 文本: {pdf_path.name}")
    try:
        info = extract_text(pdf_path)
    except Exception as e:
        return _fail("module_error", f"PDF 文本提取失败: {e}")
    text = info["text"]
    if len(text) < MIN_CHARS:
        return _fail("module_error",
                     f"PDF 可提取文本过少({len(text)}字符)，可能是扫描件/图片型PDF")
    if len(text) > MAX_CHARS:
        logger.warning(f"PDF 文本超长({len(text)})，截断至 {MAX_CHARS}")
        text = text[:MAX_CHARS]
    logger.info(f"提取完成: {info['pages']} 页, {len(text)} 字符")

    # 2. 复用视频蒸馏链路（两阶段 LLM + 同模板渲染）
    logger.info("Step 2: 两阶段知识蒸馏（复用视频链路）")
    config = AppConfig()
    video_info = VideoInfo(
        original_url=f"pdf://{pdf_path.name}",
        resolved_url="",
        platform="pdf",
        title=pdf_path.stem,
        author=pdf_path.stem,
        duration=0,
    )
    transcript = TranscriptResult(full_text=text, source="pdf_text")
    try:
        knowledge = KnowledgeDistiller(config).distill(video_info, transcript)
    except Exception as e:
        return _fail("module_error", f"LLM 蒸馏失败: {e}")

    # 3. 写入 Obsidian（output_dir 由调用方指定，当前为 Inbox）
    out_dir = Path(args.output_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = MarkdownWriter(out_dir).write(
        video_info, transcript, knowledge, noise_tags=[])

    # 修正 frontmatter 类型标记（复用视频模板，type 字段需区分开）
    content = output_path.read_text(encoding="utf-8")
    content = content.replace("type: video_distill", "type: pdf_distill", 1)
    output_path.write_text(content, encoding="utf-8")

    # 4. RAG 增量入库（A/B 级，fire-and-forget）
    _trigger_rag_ingest(output_path)

    logger.info(f"✅ Done! Output: {output_path}")
    print(f"✅ Done! Output: {output_path}")
    return 0


if __name__ == "__main__":
    exit(main())
