"""
media_followup 意图集成回放测试 (2026-08-04 P1.2)

mock _parse_intent + conversation_store + llm，验证路由分支:
1. qa 高置信 -> 基于产物笔记回答
2. reanalyze -> set_instruction 已执行(P1.3 reprocess 未上线走 ImportError 降级)
3. 低置信(<0.5) -> return False 交回 ChatHandler
4. 中置信(0.5-0.75) -> _PENDING 登记 + 确认提示
5. 无 artifact -> 提示重发链接
6. _PENDING 确认通道触发 _exec_media_followup

运行方式: python3 tests/test_media_followup_replay.py
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import commands.nl_router as nr
from core.conversation_store import ConversationStore


def _make_ctx():
    ctx = MagicMock()
    ctx.reply_message = MagicMock()
    return ctx


def _seed(store, user_id, title="狮驼岭视频", note_path="/tmp/note.md"):
    store.register_artifact(user_id, "video", url="http://douyin/xyz",
                            title=title, note_path=note_path, summary="视频摘要")


def _intent(action, conf, instruction="展开", kind="media_followup"):
    return {"intent": kind, "confidence": conf, "action": action,
            "instruction": instruction, "topic": "", "action_summary": ""}


def test_qa_high_confidence():
    os.environ.pop("CONV_STORE", None)
    with tempfile.TemporaryDirectory() as d:
        store = ConversationStore(db_path=Path(d) / "c.db")
        _seed(store, "u1")
        with patch.object(nr, "_parse_intent", return_value=_intent("qa", 0.9, "第三点展开")), \
             patch("core.conversation_store.conversation_store", store), \
             patch.object(nr.llm_client, "call_with_session", return_value="这是第三点的展开内容"):
            ctx = _make_ctx()
            r = nr.route_natural_language("刚才那个视频第三点展开讲讲", "u1", "m1", ctx)
        if r and "第三点" in ctx.reply_message.call_args[0][1]:
            print("✅ PASS: media_followup qa 高置信基于产物回答")
            return True
    print(f"❌ FAIL: r={r} reply={ctx.reply_message.call_args}")
    return False


def test_reanalyze_sets_instruction():
    os.environ.pop("CONV_STORE", None)
    with tempfile.TemporaryDirectory() as d:
        store = ConversationStore(db_path=Path(d) / "c.db")
        _seed(store, "u1", title="T")
        with patch.object(nr, "_parse_intent",
                          return_value=_intent("reanalyze", 0.92, "狮驼岭=美国")), \
             patch("core.conversation_store.conversation_store", store), \
             patch("media_handler.reprocess_with_instruction") as mock_reproc:
            ctx = _make_ctx()
            r = nr.route_natural_language("代入重新分析刚才那个视频", "u1", "m1", ctx)
            last = store.get_last_artifact("u1")
        if r and last["instruction"] == "狮驼岭=美国" and mock_reproc.called:
            print("✅ PASS: reanalyze 调 set_instruction + reprocess_with_instruction")
            return True
    print(f"❌ FAIL: r={r} instruction={last.get('instruction')} called={mock_reproc.called}")
    return False


def test_low_confidence_returns_false():
    with patch.object(nr, "_parse_intent", return_value=_intent("qa", 0.3)):
        ctx = _make_ctx()
        r = nr.route_natural_language("那个视频", "u1", "m1", ctx)
    if r is False:
        print("✅ PASS: 低置信(<0.5)交回 ChatHandler")
        return True
    print(f"❌ FAIL: r={r}")
    return False


def test_medium_confidence_pending():
    os.environ.pop("CONV_STORE", None)
    nr._PENDING.pop("u2", None)
    with tempfile.TemporaryDirectory() as d:
        store = ConversationStore(db_path=Path(d) / "c.db")
        _seed(store, "u2")
        with patch.object(nr, "_parse_intent", return_value=_intent("qa", 0.6, "展开")), \
             patch("core.conversation_store.conversation_store", store):
            ctx = _make_ctx()
            r = nr.route_natural_language("那个视频展开讲讲", "u2", "m1", ctx)
        ok = r and "u2" in nr._PENDING and "确认" in ctx.reply_message.call_args[0][1]
    nr._PENDING.pop("u2", None)
    if ok:
        print("✅ PASS: 中置信(0.5-0.75)登记 _PENDING + 确认提示")
        return True
    print(f"❌ FAIL: r={r} pending={'u2' in nr._PENDING}")
    return False


def test_no_artifact_prompts_resend():
    os.environ.pop("CONV_STORE", None)
    with tempfile.TemporaryDirectory() as d:
        store = ConversationStore(db_path=Path(d) / "c.db")  # 空库
        with patch.object(nr, "_parse_intent", return_value=_intent("qa", 0.9)), \
             patch("core.conversation_store.conversation_store", store):
            ctx = _make_ctx()
            r = nr.route_natural_language("刚才那个视频讲啥", "u3", "m1", ctx)
        if r and "重发" in ctx.reply_message.call_args[0][1]:
            print("✅ PASS: 无 artifact 提示重发链接")
            return True
    print(f"❌ FAIL: r={r}")
    return False


def test_pending_confirm_triggers_exec():
    os.environ.pop("CONV_STORE", None)
    nr._PENDING.pop("u4", None)
    with tempfile.TemporaryDirectory() as d:
        store = ConversationStore(db_path=Path(d) / "c.db")
        _seed(store, "u4")
        nr._PENDING["u4"] = {"kind": "media_followup",
                             "content": {"action": "qa", "instruction": "展开", "artifact_id": 1}}
        with patch("core.conversation_store.conversation_store", store), \
             patch.object(nr.llm_client, "call_with_session", return_value="基于笔记的回答"):
            ctx = _make_ctx()
            r = nr.route_natural_language("确认", "u4", "m1", ctx)
        ok = r and "基于笔记的回答" in ctx.reply_message.call_args[0][1] and "u4" not in nr._PENDING
    nr._PENDING.pop("u4", None)
    if ok:
        print("✅ PASS: _PENDING 确认通道触发 _exec_media_followup")
        return True
    print(f"❌ FAIL: r={r}")
    return False


def main():
    tests = [
        test_qa_high_confidence,
        test_reanalyze_sets_instruction,
        test_low_confidence_returns_false,
        test_medium_confidence_pending,
        test_no_artifact_prompts_resend,
        test_pending_confirm_triggers_exec,
    ]
    results = [t() for t in tests]
    passed = sum(results)
    print(f"\n{'=' * 40}\n{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
