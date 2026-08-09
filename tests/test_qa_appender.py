#!/usr/bin/env python3
"""qa_appender 单元测试 — 视频分析双向追问改造 Phase 1

覆盖：
1. 区段不存在时创建（插在 <details> 之前 / 无 <details> 时文末）
2. 幂等追加：重复调用序号递增（Q1 -> Q2 -> Q3）
3. 占位符「暂无」被首条问答替换
4. 含 <details> 转录块的笔记定位不误伤（转录内容原样保留）
5. 原子写：失败路径不留临时文件
6. 空问题/空回答抛 ValueError；笔记不存在抛 FileNotFoundError
7. CLI 入口冒烟

运行：~/zhiwei-shared-venv/bin/python3 tests/test_qa_appender.py
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from qa_appender import append_qa, QA_HEADING  # noqa: E402

NOTE_WITH_DETAILS = """---
title: "测试笔记"
tier: A
---

# 测试笔记

## 核心洞察
测试洞察

## 行动建议
- [ ] 测试建议

---
- 来源：youtube / 测试频道

<details>
<summary>ASR 转录原文</summary>

[00:01] 这是转录原文第一行
[00:05] 这是转录原文第二行

</details>

---
> 由知微系统 v2.0 生成
"""

NOTE_WITH_PLACEHOLDER = """# 新模板笔记

## 行动建议
- [ ] 建议

## 问答参考
暂无

## 关联资产
- 原始音频：播放
"""

NOTE_PLAIN = "# 旧笔记\n\n## 摘要\n只有摘要，没有问答区也没有 details 块。\n"


class TestQaAppender(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name, content):
        p = self.dir / name
        p.write_text(content, encoding="utf-8")
        return p

    def test_create_section_before_details(self):
        """区段不存在时创建，插在 <details> 之前，转录内容不误伤"""
        p = self._write("note1.md", NOTE_WITH_DETAILS)
        r = append_qa(p, "Cerebras 的带宽优势是多少？", "约 6260 倍 [14:48]", "feishu-doc", day="2026-08-08")
        self.assertEqual(r["qa_id"], "Q1")
        text = p.read_text(encoding="utf-8")
        self.assertIn(QA_HEADING, text)
        self.assertIn("### Q1 · 2026-08-08 · feishu-doc", text)
        self.assertIn("**问**：Cerebras 的带宽优势是多少？", text)
        self.assertIn("**答**：约 6260 倍 [14:48]", text)
        # 问答区必须在 <details> 之前
        self.assertLess(text.index(QA_HEADING), text.index("<details>"))
        # 转录原文逐行保留
        self.assertIn("[00:01] 这是转录原文第一行", text)
        self.assertIn("[00:05] 这是转录原文第二行", text)
        self.assertIn("> 由知微系统 v2.0 生成", text)

    def test_sequential_numbering(self):
        """重复追加序号递增"""
        p = self._write("note2.md", NOTE_WITH_DETAILS)
        self.assertEqual(append_qa(p, "Q一", "A一", "feishu-doc")["qa_id"], "Q1")
        self.assertEqual(append_qa(p, "Q二", "A二", "obsidian")["qa_id"], "Q2")
        self.assertEqual(append_qa(p, "Q三", "A三", "feishu-doc")["qa_id"], "Q3")
        text = p.read_text(encoding="utf-8")
        # 三条都在同一个问答区内（区段只出现一次）
        self.assertEqual(text.count(QA_HEADING), 1)
        self.assertIn("### Q3 ·", text)

    def test_placeholder_replaced(self):
        """模板占位符「暂无」被首条问答替换"""
        p = self._write("note3.md", NOTE_WITH_PLACEHOLDER)
        append_qa(p, "第一个问题", "第一个回答", "obsidian")
        text = p.read_text(encoding="utf-8")
        section = text[text.index(QA_HEADING):]
        self.assertNotIn("暂无", section.split("## 关联资产")[0])
        self.assertIn("### Q1 ·", text)
        # 问答区后面的章节保持原位
        self.assertIn("## 关联资产", text)

    def test_append_into_existing_section_keeps_following_sections(self):
        """追加进已有问答区时，其后的二级标题章节不被吞掉"""
        p = self._write("note4.md", NOTE_WITH_PLACEHOLDER)
        append_qa(p, "问1", "答1", "obsidian")
        append_qa(p, "问2", "答2", "feishu-doc")
        text = p.read_text(encoding="utf-8")
        self.assertIn("## 关联资产", text)
        self.assertIn("**问**：问2", text)

    def test_plain_note_append_at_end(self):
        """无问答区无 <details> 的旧笔记：文末创建"""
        p = self._write("note5.md", NOTE_PLAIN)
        append_qa(p, "旧笔记提问", "旧笔记回答", "obsidian")
        text = p.read_text(encoding="utf-8")
        self.assertIn(QA_HEADING, text)
        self.assertIn("**问**：旧笔记提问", text)

    def test_no_temp_files_left(self):
        """成功写入后目录内无残留临时文件"""
        p = self._write("note6.md", NOTE_WITH_DETAILS)
        append_qa(p, "问题", "回答", "obsidian")
        leftovers = [f for f in self.dir.iterdir() if f.suffix == ".tmp"]
        self.assertEqual(leftovers, [])

    def test_invalid_inputs(self):
        p = self._write("note7.md", NOTE_PLAIN)
        with self.assertRaises(ValueError):
            append_qa(p, "", "回答", "obsidian")
        with self.assertRaises(ValueError):
            append_qa(p, "问题", "   ", "obsidian")
        with self.assertRaises(FileNotFoundError):
            append_qa(self.dir / "不存在.md", "问题", "回答", "obsidian")

    def test_multiline_answer(self):
        """多行回答保持格式"""
        p = self._write("note8.md", NOTE_WITH_DETAILS)
        append_qa(p, "多行问题", "第一行结论\n第二行论据 [03:37]", "feishu-doc")
        text = p.read_text(encoding="utf-8")
        self.assertIn("第一行结论\n第二行论据 [03:37]", text)

    def test_cli_entry(self):
        """CLI 冒烟：python3 qa_appender.py <note> --q --a --source"""
        p = self._write("note9.md", NOTE_WITH_DETAILS)
        qa_path = Path(__file__).resolve().parent.parent / "qa_appender.py"
        r = subprocess.run(
            [sys.executable, str(qa_path), str(p),
             "--q", "CLI 问题", "--a", "CLI 回答", "--source", "obsidian",
             "--date", "2026-08-08"],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("OK Q1", r.stdout)
        self.assertIn("### Q1 · 2026-08-08 · obsidian", p.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
