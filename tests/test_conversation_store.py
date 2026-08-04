"""
conversation_store 模块测试 (2026-08-04 P1.1)

验证:
1. record_turn 写入与 50 轮裁剪
2. register_artifact / get_last_artifact (含 kind 过滤)
3. build_context 长度上限与空库返回 ''
4. set_instruction
5. CONV_STORE=0 全程 no-op(建表/写入/查询均不执行,DB 文件不创建)

运行方式: python3 tests/test_conversation_store.py
"""

import os
import sys
import tempfile
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.conversation_store import ConversationStore


def _new_store(db_path):
    """构造一个启用状态的 store（确保 CONV_STORE 未关闭）"""
    os.environ.pop("CONV_STORE", None)
    return ConversationStore(db_path=db_path)


def test_record_turn_and_prune():
    with tempfile.TemporaryDirectory() as d:
        s = _new_store(Path(d) / "c.db")
        for i in range(55):
            s.record_turn("u1", "user", f"msg{i}")
        with s._connect() as c:
            n = c.execute("SELECT COUNT(*) FROM turns WHERE user_id=?", ("u1",)).fetchone()[0]
            first = c.execute(
                "SELECT content FROM turns WHERE user_id=? ORDER BY id ASC LIMIT 1",
                ("u1",)).fetchone()[0]
        if n == 50 and first == "msg5":
            print("✅ PASS: record_turn 50 轮裁剪")
            return True
        print(f"❌ FAIL: n={n} first={first}")
        return False


def test_register_and_get_last_artifact():
    with tempfile.TemporaryDirectory() as d:
        s = _new_store(Path(d) / "c.db")
        aid1 = s.register_artifact("u1", "video", url="http://a", title="A",
                                   note_path="/a.md", summary="sa")
        aid2 = s.register_artifact("u1", "article", url="http://b", title="B")
        last = s.get_last_artifact("u1")
        last_video = s.get_last_artifact("u1", kind="video")
        if (isinstance(aid1, int) and isinstance(aid2, int) and aid2 > aid1
                and last["title"] == "B" and last["kind"] == "article"
                and last_video["title"] == "A"):
            print("✅ PASS: register/get_last_artifact (含 kind 过滤)")
            return True
        print(f"❌ FAIL: aid1={aid1} aid2={aid2} last={last} last_video={last_video}")
        return False


def test_build_context_empty_and_cap():
    with tempfile.TemporaryDirectory() as d:
        s = _new_store(Path(d) / "c.db")
        if s.build_context("nobody") != "":
            print("❌ FAIL: 空库应返回 ''")
            return False
        s.register_artifact("u1", "video", title="T", summary="s" * 100)
        s.record_turn("u1", "user", "x" * 500)
        s.record_turn("u1", "assistant", "y" * 500)
        ctx = s.build_context("u1", max_chars=300)
        if len(ctx) <= 300 and "最近处理的内容" in ctx and "对话历史" in ctx:
            print("✅ PASS: build_context 空库与长度上限")
            return True
        print(f"❌ FAIL: len={len(ctx)} ctx={ctx[:80]!r}")
        return False


def test_set_instruction():
    with tempfile.TemporaryDirectory() as d:
        s = _new_store(Path(d) / "c.db")
        aid = s.register_artifact("u1", "video", title="T")
        s.set_instruction(aid, "狮驼岭=美国")
        last = s.get_last_artifact("u1")
        if last["instruction"] == "狮驼岭=美国":
            print("✅ PASS: set_instruction")
            return True
        print(f"❌ FAIL: instruction={last.get('instruction')}")
        return False


def test_conv_store_disabled_noop():
    with tempfile.TemporaryDirectory() as d:
        os.environ["CONV_STORE"] = "0"
        try:
            s = ConversationStore(db_path=Path(d) / "off.db")
            r1 = s.register_artifact("u1", "video")
            r2 = s.get_last_artifact("u1")
            r3 = s.build_context("u1")
            r4 = s.record_turn("u1", "user", "x")
            created = (Path(d) / "off.db").exists()
        finally:
            os.environ.pop("CONV_STORE", None)
        if r1 is None and r2 is None and r3 == "" and r4 is None and not created:
            print("✅ PASS: CONV_STORE=0 全程 no-op(含建表不执行)")
            return True
        print(f"❌ FAIL: r1={r1} r2={r2} r3={r3!r} created={created}")
        return False


def main():
    tests = [
        test_record_turn_and_prune,
        test_register_and_get_last_artifact,
        test_build_context_empty_and_cap,
        test_set_instruction,
        test_conv_store_disabled_noop,
    ]
    results = [t() for t in tests]
    passed = sum(results)
    print(f"\n{'=' * 40}\n{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
