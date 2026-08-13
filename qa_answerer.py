#!/usr/bin/env python3
"""问答回答生成器（qa_answerer）— Obsidian 追问方案（方式 C）核心

Templater user script 经 child_process 调用本脚本生成追问回答：
与 scheduler jobs_feishu_doc_qa.build_answer 同任务同 prompt 同模型通道
（deep_analysis auto 链，Coding Plan 包月主力 kimi-k2.5），保证飞书侧与
Obsidian 侧回答质量同源。

用法：
    ~/zhiwei-shared-venv/bin/python3 qa_answerer.py --note <笔记.md> --question "问题"

输出：stdout 打印回答正文（成功）；失败时 stderr 输出原因并非 0 退出。
"""

import argparse
import sys
from pathlib import Path

NOTE_MAX_CHARS = 40000  # 与 jobs_feishu_doc_qa.NOTE_MAX_CHARS 对齐


def build_answer(note_text: str, question: str, title: str) -> str:
    """基于笔记全文（含 ASR 转录）生成带时间戳引用的回答。

    prompt 与 jobs_feishu_doc_qa.build_answer 保持对齐，修改时两处同步。
    """
    from zhiwei_common.llm import LLMClient
    prompt = (
        f"【背景：视频蒸馏笔记《{title}》全文（含 ASR 转录原文）】\n"
        f"{note_text[:NOTE_MAX_CHARS]}\n\n"
        f"【用户追问】{question}\n\n"
        "请基于上述笔记内容回答：\n"
        "1. 结论先行，直接回答问题；\n"
        "2. 引用转录原文时附 [MM:SS] 时间戳；\n"
        "3. 笔记中没有的信息要明确说明「笔记未覆盖」，不要编造；\n"
        "4. 控制在 500 字以内。"
    )
    # deep_analysis auto 链（Coding Plan 包月主力 kimi-k2.5 → deepseek-v4-pro 兜底；火山包月已退役 2026-08-12）
    # 用户主动交互场景；with_session 变体直接返回字符串（失败以 ❌ 开头）
    client = LLMClient()
    answer = client.call_by_task_with_session(
        task="deep_analysis", message=prompt,
        session_id=f"obsidian-qa-{abs(hash(title)) % 10**8}")
    if answer and not str(answer).startswith("❌"):
        return str(answer).strip()
    raise RuntimeError(f"模型回答生成失败: {str(answer)[:150]}")


def main():
    parser = argparse.ArgumentParser(description="基于视频笔记生成追问回答")
    parser.add_argument("--note", required=True, help="Vault 笔记路径")
    parser.add_argument("--question", required=True, help="追问问题")
    args = parser.parse_args()

    note = Path(args.note).expanduser()
    if not note.exists():
        print(f"笔记不存在: {note}", file=sys.stderr)
        return 1
    try:
        note_text = note.read_text(errors="ignore")
    except Exception as e:
        print(f"笔记读取失败: {e}", file=sys.stderr)
        return 1

    try:
        answer = build_answer(note_text, args.question, note.stem)
    except Exception as e:
        print(f"回答生成失败: {e}", file=sys.stderr)
        return 1

    print(answer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
