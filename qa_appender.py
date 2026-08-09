#!/usr/bin/env python3
"""问答沉淀追加器（qa_appender）— 视频分析双向追问改造 Phase 1

纯函数式库：输入输出只有文件路径与字符串，不 import bot/scheduler 任何模块、
不依赖飞书——将来在笔记本从机侧同样可用（迁移设计约束第 4 条）。

职责：把一条问答按统一格式追加进 Vault 笔记的「## 问答参考」区。
- 区段不存在时动态创建（旧笔记兼容，插在 ASR <details> 块之前，无则文末）
- 序号幂等：扫描区内已有 ### Q{n} 取最大值递增
- 原子写：同目录临时文件 + os.replace，中断不留半文件

格式约定（与 douyin_distiller FOOTER 占位段一致）：

    ## 问答参考
    ### Q1 · 2026-08-08 · feishu-doc
    **问**：<问题>

    **答**：<回答，引用原文时带 [MM:SS] 时间戳>

用法（库）：
    from qa_appender import append_qa
    append_qa(note_path, question, answer, source="feishu-doc")

用法（CLI，供 Obsidian 侧强模型/手工补录）：
    python3 qa_appender.py <笔记.md> --q "问题" --a "回答" --source obsidian
"""

import argparse
import os
import re
import sys
import tempfile
from datetime import date
from pathlib import Path

QA_HEADING = "## 问答参考"
PLACEHOLDER = "暂无"
_ENTRY_RE = re.compile(r"^### Q(\d+)", re.M)
_DETAILS_RE = re.compile(r"^<details>\s*$", re.M)


def _find_section(text: str) -> tuple:
    """定位「## 问答参考」区。返回 (start, end)；不存在返回 (None, None)。

    区段范围：heading 行之后，到下一个二级标题（`## `）或文末。
    """
    m = re.search(r"^## 问答参考\s*$", text, re.M)
    if not m:
        return None, None
    start = m.end()
    nxt = re.search(r"^## ", text[start:], re.M)
    end = start + nxt.start() if nxt else len(text)
    return start, end


def _next_index(section_body: str) -> int:
    nums = [int(x) for x in _ENTRY_RE.findall(section_body)]
    return (max(nums) + 1) if nums else 1


def _build_entry(idx: int, question: str, answer: str, source: str, day: str) -> str:
    return (
        f"### Q{idx} · {day} · {source}\n"
        f"**问**：{question.strip()}\n\n"
        f"**答**：{answer.strip()}\n"
    )


def append_qa(note_path, question: str, answer: str, source: str,
              day: str = None) -> dict:
    """向笔记追加一条问答，返回 {"qa_id": "Q{n}", "note_path": ...}。

    Args:
        note_path: Vault 笔记路径（str 或 Path）
        question: 问题文本
        answer: 回答文本
        source: 来源标记（feishu-doc / obsidian / feishu-chat 等）
        day: 日期字符串 YYYY-MM-DD，默认今天

    Raises:
        FileNotFoundError: 笔记不存在
    """
    if not question or not str(question).strip():
        raise ValueError("question 不能为空")
    if not answer or not str(answer).strip():
        raise ValueError("answer 不能为空")

    note = Path(os.path.expanduser(str(note_path)))
    if not note.exists():
        raise FileNotFoundError(f"笔记不存在: {note}")

    text = note.read_text(encoding="utf-8")
    day = day or date.today().isoformat()

    start, end = _find_section(text)
    if start is None:
        # 旧笔记无问答区：插在 ASR <details> 块之前，无则文末
        entry_idx = 1
        section = f"\n{QA_HEADING}\n\n{_build_entry(entry_idx, question, answer, source, day)}\n"
        dm = _DETAILS_RE.search(text)
        if dm:
            insert_at = dm.start()
            # 与 <details> 之间留分隔线，保持笔记结构清晰
            new_text = text[:insert_at] + section + "\n---\n\n" + text[insert_at:]
        else:
            new_text = text.rstrip("\n") + "\n" + section
    else:
        body = text[start:end]
        entry_idx = _next_index(body)
        # 去掉占位符「暂无」
        body = re.sub(rf"^\s*{PLACEHOLDER}\s*$", "", body, count=1, flags=re.M)
        body = body.rstrip("\n")
        new_body = (
            ("\n" if not body else body + "\n")
            + "\n" + _build_entry(entry_idx, question, answer, source, day) + "\n"
        )
        new_text = text[:start] + new_body + text[end:]

    # 原子写：同目录临时文件 + os.replace
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{note.name}.", suffix=".tmp", dir=str(note.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_text)
        os.replace(tmp_path, note)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return {"qa_id": f"Q{entry_idx}", "note_path": str(note)}


def main():
    parser = argparse.ArgumentParser(description="向笔记「问答参考」区追加一条问答")
    parser.add_argument("note", help="Vault 笔记路径")
    parser.add_argument("--q", required=True, dest="question", help="问题")
    parser.add_argument("--a", required=True, dest="answer", help="回答")
    parser.add_argument("--source", default="obsidian",
                        help="来源标记（feishu-doc/obsidian 等，默认 obsidian）")
    parser.add_argument("--date", default=None, help="日期 YYYY-MM-DD，默认今天")
    args = parser.parse_args()

    result = append_qa(args.note, args.question, args.answer,
                       source=args.source, day=args.date)
    print(f"OK {result['qa_id']} -> {result['note_path']}")


if __name__ == "__main__":
    main()
