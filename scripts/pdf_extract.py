#!/usr/bin/env python3
"""PDF 文本提取（需 pymupdf，必须用 zhiwei-rag/venv 运行）

shared-venv 无 pymupdf，沿用「调 rag venv」的既有惯例（2026-08-02）。
每页文本前加 [pN] 页码锚点，供蒸馏 LLM 充当定位参照（替代视频时间戳）。

用法: <rag-venv-python> pdf_extract.py <file.pdf>
输出: stdout JSON {"pages": N, "chars": M, "text": "..."}
"""
import json
import sys


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "usage: pdf_extract.py <file.pdf>"}))
        sys.exit(1)

    import fitz  # pymupdf

    doc = fitz.open(sys.argv[1])
    parts = []
    for i, page in enumerate(doc):
        text = page.get_text().strip()
        if text:
            parts.append(f"[p{i + 1}] {text}")
    full = "\n\n".join(parts)
    print(json.dumps({"pages": doc.page_count, "chars": len(full), "text": full},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
