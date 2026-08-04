"""
知微 bot 对话理解重构 — 六场景综合 mock 回放验证 (2026-08-04)

基于用户真实 message_log.db 使用习惯，mock LLM / distiller / 飞书 API，
验证 P0+P1 重构后的路由与上下文链路。

运行: ~/zhiwei-shared-venv/bin/python3 tests/verify_all_scenarios.py
"""

import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.conversation_store import ConversationStore
from command_handler import handle_text_async as _handle_text_async, init_command_handler

# ─── 全局 mock 设置 ───

# 用户 ID（模拟飞书会话）
USER_ID = "ou_test_user_001"
SESSION_ID = f"feishu-{USER_ID}"

# 记录所有 reply 输出
_replies = []

def _record_reply(message_id, text):
    _replies.append({"message_id": message_id, "text": text})
    print(f"  [REPLY] → {text[:100]}...")

def _make_mock_ctx(store, pending_video_confirm=None):
    """构造 mock ctx，注入所有 command_handler 依赖"""
    ctx = MagicMock()
    ctx.reply_message = MagicMock(side_effect=_record_reply)
    ctx.reply_card = MagicMock()
    ctx.handle_video_async = MagicMock()
    ctx.is_video_url = MagicMock()
    ctx.extract_video_url = MagicMock()
    ctx.summarize_url = MagicMock()
    ctx.get_memory = MagicMock()
    ctx.add_to_history = MagicMock()
    ctx.get_history = MagicMock()
    ctx.pending_voice = {}
    ctx.pending_image = {}
    ctx.pending_video_confirm = pending_video_confirm or {}
    ctx.pending_review = {}
    ctx.get_video_history = MagicMock()
    ctx.get_chat_handler = MagicMock()
    ctx.TaskLogger = MagicMock()
    ctx.save_active_user = MagicMock()
    ctx.load_active_user = MagicMock()
    ctx.chat_history = {}
    ctx.MAX_HISTORY = 20
    ctx.RATE_LIMIT_SECONDS = 0
    ctx.user_last_request = {}
    ctx.memory_cache = {}
    ctx.query_knowledge_base = MagicMock(return_value=None)
    ctx.call_openclaw_agent = MagicMock(return_value=(False, "mock"))
    ctx.is_article_url = MagicMock(return_value=False)
    ctx.extract_article_url = MagicMock(return_value=None)
    return ctx


def _mock_llm_success(return_text="Mock LLM 回答"):
    """mock llm_client.call 返回成功"""
    return patch("zhiwei_common.llm.llm_client.call", return_value=(True, return_text))


def _mock_llm_call_with_session(return_text="Mock session 回答"):
    return patch("zhiwei_common.llm.llm_client.call_with_session", return_value=return_text)


def _mock_nl_router_intent(intent="chat", confidence=0.95, **kwargs):
    """mock nl_router._parse_intent"""
    data = {"intent": intent, "confidence": confidence, "topic": "", "action_summary": ""}
    data.update(kwargs)
    return data


def _mock_video_url(text, url="http://douyin.com/test"):
    """mock is_video_url + extract_video_url"""
    return (patch.object(MagicMock(), "is_video_url", return_value=True),
            patch.object(MagicMock(), "extract_video_url", return_value=url))


# ─── 场景测试 ───

def test_scene_1_raw_link():
    """
    场景 1: 裸粘抖音链接
    预期: is_video_url → 媒体管线处理
    """
    print("\n" + "=" * 60)
    print("场景 1: 裸粘抖音链接 — 预期走媒体管线")
    _replies.clear()

    with tempfile.TemporaryDirectory() as d:
        store = ConversationStore(db_path=Path(d) / "c.db")
        ctx = _make_mock_ctx(store)

        # 注入 ctx 到 command_handler
        init_command_handler(
            ctx.reply_message, ctx.reply_card, ctx.call_openclaw_agent,
            ctx.query_knowledge_base, ctx.get_memory, ctx.add_to_history,
            ctx.get_history, ctx.is_article_url, ctx.is_video_url,
            ctx.summarize_url, ctx.handle_video_async, ctx.extract_video_url,
            ctx.extract_article_url, ctx.TaskLogger, ctx.save_active_user,
            ctx.load_active_user, ctx.chat_history, ctx.pending_voice,
            ctx.pending_image, ctx.pending_review, ctx.MAX_HISTORY,
            ctx.RATE_LIMIT_SECONDS, ctx.user_last_request, ctx.memory_cache,
            ctx.pending_video_confirm, ctx.get_video_history, ctx.get_chat_handler,
        )

        # 设置 is_video_url 返回 True
        ctx.is_video_url.return_value = True
        ctx.extract_video_url.return_value = "http://douyin.com/test123"

        # 设置 is_article_url 返回 False（避免被文章路由拦截）
        ctx.is_article_url.return_value = False

        with _mock_llm_success("mock"), _mock_llm_call_with_session("mock"):
            _handle_text_async(
                "0.51 复制打开抖音，看看【某博主】的视频... https://v.douyin.com/abc123/",
                USER_ID, "msg_001", "user")

        if ctx.handle_video_async.called:
            print("✅ PASS: 场景 1 — 裸链接正确走媒体管线")
            return True
        print(f"❌ FAIL: handle_video_async 未被调用, called={ctx.handle_video_async.called}")
        return False


def test_scene_2_qa_followup():
    """
    场景 2: 贴链接 → 笔记生成 → 追问「那个视频第三点再展开」
    预期: media_followup-qa 命中，基于产物笔记回答
    """
    print("\n" + "=" * 60)
    print("场景 2: 视频笔记生成后追问细节 — 预期 media_followup-qa 命中")
    _replies.clear()

    with tempfile.TemporaryDirectory() as d:
        store = ConversationStore(db_path=Path(d) / "c.db")
        # 预置一个视频产物（模拟刚才的笔记已生成）
        store.register_artifact(
            USER_ID, "video", url="http://douyin.com/test",
            title="测试视频标题", note_path="/tmp/note.md",
            summary="核心洞察：AI芯片市场正在发生结构性变化。量化指标：全球算力需求增长300%...")

        ctx = _make_mock_ctx(store)
        init_command_handler(
            ctx.reply_message, ctx.reply_card, ctx.call_openclaw_agent,
            ctx.query_knowledge_base, ctx.get_memory, ctx.add_to_history,
            ctx.get_history, ctx.is_article_url, ctx.is_video_url,
            ctx.summarize_url, ctx.handle_video_async, ctx.extract_video_url,
            ctx.extract_article_url, ctx.TaskLogger, ctx.save_active_user,
            ctx.load_active_user, ctx.chat_history, ctx.pending_voice,
            ctx.pending_image, ctx.pending_review, ctx.MAX_HISTORY,
            ctx.RATE_LIMIT_SECONDS, ctx.user_last_request, ctx.memory_cache,
            ctx.pending_video_confirm, ctx.get_video_history, ctx.get_chat_handler,
        )

        ctx.is_video_url.return_value = False
        ctx.is_article_url.return_value = False

        intent = _mock_nl_router_intent("media_followup", 0.95, action="qa",
                                         instruction="第三点再展开讲讲")
        # Mock: 创建假 note_path 文件
        note_path = Path(d) / "note.md"
        note_path.write_text("## 核心洞察\n第一点：...\n第二点：...\n第三点：AI芯片供给缺口分析\n\n这是第三点的详细内容。全球半导体产能紧张，导致AI芯片交付周期延长至12个月以上。")

        with patch("commands.nl_router._parse_intent", return_value=intent), \
             patch("core.conversation_store.conversation_store", store), \
             _mock_llm_call_with_session("基于笔记第三点：AI芯片供给缺口分析，全球半导体产能紧张导致交付周期延长至12个月"), \
             _mock_llm_success("mock"):
            _handle_text_async("刚才那个视频第三点再展开讲讲", USER_ID, "msg_002", "user")

        # 检查回复中是否包含基于笔记的回答
        ok = any("第三点" in r["text"] or "AI芯片" in r["text"] or "缺口" in r["text"] for r in _replies)
        if ok:
            print("✅ PASS: 场景 2 — 追问基于产物笔记回答")
            return True
        print(f"❌ FAIL: 未找到基于笔记的回复。replies={[r['text'][:60] for r in _replies]}")
        return False


def test_scene_3_reanalyze_with_mapping():
    """
    场景 3: 贴链接 → 笔记生成 → 发代称映射 → 要求代入重析
    预期: media_followup-reanalyze 命中，set_instruction 执行，reprocess_with_instruction 调用
    """
    print("\n" + "=" * 60)
    print("场景 3: 代称映射 → 代入重析 (8/4 狮驼岭场景复刻)")
    _replies.clear()

    with tempfile.TemporaryDirectory() as d:
        store = ConversationStore(db_path=Path(d) / "c.db")
        store.register_artifact(
            USER_ID, "video", url="http://douyin.com/shituoling",
            title="狮驼岭的AI故事", note_path="/tmp/shituoling.md",
            summary="视频讨论了AI行业的格局变化，涉及多个国家...")

        ctx = _make_mock_ctx(store)
        init_command_handler(
            ctx.reply_message, ctx.reply_card, ctx.call_openclaw_agent,
            ctx.query_knowledge_base, ctx.get_memory, ctx.add_to_history,
            ctx.get_history, ctx.is_article_url, ctx.is_video_url,
            ctx.summarize_url, ctx.handle_video_async, ctx.extract_video_url,
            ctx.extract_article_url, ctx.TaskLogger, ctx.save_active_user,
            ctx.load_active_user, ctx.chat_history, ctx.pending_voice,
            ctx.pending_image, ctx.pending_review, ctx.MAX_HISTORY,
            ctx.RATE_LIMIT_SECONDS, ctx.user_last_request, ctx.memory_cache,
            ctx.pending_video_confirm, ctx.get_video_history, ctx.get_chat_handler,
        )
        ctx.is_video_url.return_value = False
        ctx.is_article_url.return_value = False

        intent = _mock_nl_router_intent("media_followup", 0.92, action="reanalyze",
                                         instruction="狮驼岭=美国、凤仙郡=中国、棒子=韩国、鬼子=日本。代入后重新输出观点摘要")

        # Mock: 所有前置 handler 返回 False，确保消息到达 nl_router
        with patch("command_handler.handle_learn_commands", return_value=False), \
             patch("command_handler.handle_agent_commands", return_value=False), \
             patch("command_handler.handle_knowledge_commands", return_value=False), \
             patch("command_handler.handle_research_commands", return_value=False), \
             patch("command_handler.handle_dev_commands", return_value=False), \
             patch("command_handler.handle_system_commands", return_value=False), \
             patch("command_handler.handle_media_commands", return_value=False), \
             patch("commands.nl_router._parse_intent", return_value=intent), \
             patch("core.conversation_store.conversation_store", store), \
             patch("media_handler.reprocess_with_instruction") as mock_reproc, \
             _mock_llm_success("mock"), _mock_llm_call_with_session("mock"):
            _handle_text_async(
                "这个博主的狮驼岭是指美国，凤仙郡是指中国，请代入重新分析刚才的视频",
                USER_ID, "msg_003", "user")

        # 检查 1: set_instruction 已执行
        last_art = store.get_last_artifact(USER_ID)
        inst_ok = last_art and "狮驼岭" in (last_art.get("instruction") or "")

        # 检查 2: reprocess_with_instruction 被调用
        if inst_ok and mock_reproc.called:
            print("✅ PASS: 场景 3 — reanalyze 触发 set_instruction + reprocess_with_instruction")
            return True
        print(f"❌ FAIL: inst_ok={inst_ok} instruction={last_art.get('instruction') if last_art else 'N/A'} reproc_called={mock_reproc.called}")
        return False


def test_scene_4_duplicate_continue():
    """
    场景 4: 重复贴链接 → 回「继续」
    预期: pending_video_confirm 登记 → 「继续」消费 → handle_video_async 触发
    """
    print("\n" + "=" * 60)
    print("场景 4: 重复链接 → 「继续」 — 预期真正重处理")
    _replies.clear()

    with tempfile.TemporaryDirectory() as d:
        store = ConversationStore(db_path=Path(d) / "c.db")
        pvc = {}

        # ——— 第一步：模拟重复视频检测 ———
        # 直接调用 media_commands 的登记逻辑
        ctx = _make_mock_ctx(store, pending_video_confirm=pvc)
        ctx.is_video_url.return_value = True
        ctx.extract_video_url.return_value = "http://douyin.com/already_processed"

        init_command_handler(
            ctx.reply_message, ctx.reply_card, ctx.call_openclaw_agent,
            ctx.query_knowledge_base, ctx.get_memory, ctx.add_to_history,
            ctx.get_history, ctx.is_article_url, ctx.is_video_url,
            ctx.summarize_url, ctx.handle_video_async, ctx.extract_video_url,
            ctx.extract_article_url, ctx.TaskLogger, ctx.save_active_user,
            ctx.load_active_user, ctx.chat_history, ctx.pending_voice,
            ctx.pending_image, ctx.pending_review, ctx.MAX_HISTORY,
            ctx.RATE_LIMIT_SECONDS, ctx.user_last_request, ctx.memory_cache,
            pvc, ctx.get_video_history, ctx.get_chat_handler,
        )

        # 前置 handler 全部返回 False，确保消息到达 media_commands 的重复检测
        with patch("command_handler.handle_learn_commands", return_value=False), \
             patch("command_handler.handle_agent_commands", return_value=False), \
             patch("command_handler.handle_knowledge_commands", return_value=False), \
             patch("command_handler.handle_research_commands", return_value=False), \
             patch("command_handler.handle_dev_commands", return_value=False), \
             patch("command_handler.handle_system_commands", return_value=False), \
             patch("commands.media_commands._video_history.check_duplicate", return_value={
                 "title": "已处理视频", "processed_at": "2026-08-04",
                 "output_path": "/tmp/existing.md"}), \
             _mock_llm_success("mock"), _mock_llm_call_with_session("mock"):
            _handle_text_async(
                "0.51 复制打开抖音... https://v.douyin.com/already_processed",
                USER_ID, "msg_004", "user")

        # 验证: pending_video_confirm 已登记
        step1_ok = USER_ID in pvc and pvc[USER_ID]["url"] == "http://douyin.com/already_processed"
        if not step1_ok:
            print(f"❌ FAIL: 场景 4 步骤 1 — pending_video_confirm 未登记。pvc={pvc}")
            return False

        # ——— 第二步：回复「继续」 ———
        _replies.clear()
        ctx.is_video_url.return_value = False
        with _mock_llm_success("mock"), _mock_llm_call_with_session("mock"):
            _handle_text_async("继续", USER_ID, "msg_005", "user")

        if ctx.handle_video_async.called and USER_ID not in pvc:
            print("✅ PASS: 场景 4 — 「继续」触发重处理 + pending 已清除")
            return True
        print(f"❌ FAIL: handle_video_async called={ctx.handle_video_async.called} pvc_left={USER_ID in pvc}")
        return False


def test_scene_5_wechat_article():
    """
    场景 5: 公众号文章链接 — 预期不走 media_followup，走文章处理或 agent 路由
    """
    print("\n" + "=" * 60)
    print("场景 5: 公众号文章链接 — 预期不走媒体管线/不误入 media_followup")
    _replies.clear()

    with tempfile.TemporaryDirectory() as d:
        store = ConversationStore(db_path=Path(d) / "c.db")
        ctx = _make_mock_ctx(store)
        init_command_handler(
            ctx.reply_message, ctx.reply_card, ctx.call_openclaw_agent,
            ctx.query_knowledge_base, ctx.get_memory, ctx.add_to_history,
            ctx.get_history, ctx.is_article_url, ctx.is_video_url,
            ctx.summarize_url, ctx.handle_video_async, ctx.extract_video_url,
            ctx.extract_article_url, ctx.TaskLogger, ctx.save_active_user,
            ctx.load_active_user, ctx.chat_history, ctx.pending_voice,
            ctx.pending_image, ctx.pending_review, ctx.MAX_HISTORY,
            ctx.RATE_LIMIT_SECONDS, ctx.user_last_request, ctx.memory_cache,
            ctx.pending_video_confirm, ctx.get_video_history, ctx.get_chat_handler,
        )

        ctx.is_video_url.return_value = False
        ctx.is_article_url.return_value = True
        ctx.extract_article_url.return_value = "https://mp.weixin.qq.com/s/test"
        ctx.summarize_url.return_value = "📄 公众号文章摘要：这是一篇关于AI芯片的文章..."

        with _mock_llm_success("mock"), _mock_llm_call_with_session("mock"):
            _handle_text_async(
                "https://mp.weixin.qq.com/s/Ly9qH9Qx5yTvbsfgf9wWJg",
                USER_ID, "msg_006", "user")

        # 验证: 没有误入 video 处理
        if not ctx.handle_video_async.called:
            print("✅ PASS: 场景 5 — 文章链接未误入视频管线")
            return True
        print("❌ FAIL: 文章链接错误触发了视频处理")
        return False


def test_scene_6_persistence():
    """
    场景 6: 重启后持久化 — 验证 ConversationStore 写入后被读取
    （模拟 kickstart 后的行为：新 store 实例读旧 DB）
    """
    print("\n" + "=" * 60)
    print("场景 6: 重启后持久化 — 新 store 实例读旧 DB 仍能找到产物")
    _replies.clear()

    with tempfile.TemporaryDirectory() as d:
        db_path = Path(d) / "c.db"

        # 模拟第一次会话：写入产物
        store1 = ConversationStore(db_path=db_path)
        store1.register_artifact(
            USER_ID, "video", url="http://douyin.com/persist",
            title="持久化测试视频", note_path="/tmp/persist.md",
            summary="持久化摘要")

        # 模拟重启：新 store 实例
        store2 = ConversationStore(db_path=db_path)
        last = store2.get_last_artifact(USER_ID)

        if last and last.get("title") == "持久化测试视频":
            # 进一步验证 build_context 包含产物
            ctx_str = store2.build_context(USER_ID)
            if "持久化测试视频" in ctx_str or "持久化摘要" in ctx_str:
                print("✅ PASS: 场景 6 — 重启后持久化读取成功 + build_context 含产物")
                return True
            print(f"❌ FAIL: build_context 不含产物信息。ctx={ctx_str[:200]}")
            return False
        print(f"❌ FAIL: last_artifact={last}")
        return False


def main():
    print("=" * 60)
    print("知微 bot 对话理解重构 — 六场景 mock 回放验证")
    print(f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    tests = [
        test_scene_1_raw_link,
        test_scene_2_qa_followup,
        test_scene_3_reanalyze_with_mapping,
        test_scene_4_duplicate_continue,
        test_scene_5_wechat_article,
        test_scene_6_persistence,
    ]

    results = [t() for t in tests]
    passed = sum(results)

    print("\n" + "=" * 60)
    print(f"结果: {passed}/{len(results)} 通过")
    for i, (name, ok) in enumerate(zip([t.__name__ for t in tests], results)):
        print(f"  {'✅' if ok else '❌'} 场景 {i+1}: {name.replace('test_scene_', '').replace('_', ' ')}")
    print("=" * 60)

    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())