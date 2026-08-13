from zhiwei_common.llm import llm_client
import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# ============ 共享函数（NL 路由器与斜杠命令共用，2026-07-26 P1） ============


def do_capture(content: str, user_id: str, source: str = "飞书 /insight") -> tuple:
    """把灵感/知识片段写入 KarpathyVault/_raw/

    Returns: (success, filepath_or_error, filename)
    """
    vault_path = Path.home() / "Documents" / "KarpathyVault" / "_raw"
    vault_path.mkdir(parents=True, exist_ok=True)

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    filename = f"raw_insight_{timestamp}.md"
    filepath = vault_path / filename

    file_content = f"""# 灵感捕获

> 时间: {time.strftime("%Y-%m-%d %H:%M:%S")}
> 来源: {source}
> 用户: {user_id[:8] if len(user_id) > 8 else user_id}

{content}
"""
    try:
        filepath.write_text(file_content, encoding="utf-8")
        return True, str(filepath), filename
    except Exception as e:
        return False, str(e), filename


def _send_matched_figures(query: str, message_id: str, ctx, top_k: int = 5,
                         max_images: int = 2) -> int:
    """⭐ F4 2026-07-31: /ask 命中图表 chunk 时，把原图一并回传飞书

    背景：RAG 里已有大量图表描述 chunk（source 形如
    pdf_image:2605.22035.pdf_p0_1024x1262），但图片本身当时未落盘，
    用户只能读到文字描述。现命中时用 rag venv 现场从 PDF 提图
    （shared-venv 无 pymupdf，故沿用“调 rag venv”的既有惯例），
    再走 zhiwei_common.pusher 上传发送。任何环节失败均静默跳过，不影响问答。

    Returns: 成功发出的图片数
    """
    import subprocess
    import tempfile
    from pathlib import Path

    sent = 0
    try:
        from core.rag_client import get_rag_client
        results = get_rag_client().search(query, top_k=top_k) or []
        figures = [r for r in results
                   if str(r.get("source") or "").startswith("pdf_image:")][:max_images]
        if not figures:
            return 0

        rag_python = Path.home() / "zhiwei-rag" / "venv" / "bin" / "python3"
        script = Path.home() / "zhiwei-rag" / "scripts" / "extract_pdf_page_image.py"
        if not (rag_python.exists() and script.exists()):
            return 0

        from zhiwei_common.pusher import FeishuPusher
        import os as _os
        # 2026-07-31: 凭据优先环境变量（W3.2 已将 scheduler settings.yaml 明文占位化，
        # 值由 SCHEDULER_* / load_secrets 注入）；yaml 仅作旧部署兼容回退。
        from zhiwei_common.secrets import load_secrets
        load_secrets(silent=True)
        app_id = (_os.getenv("SCHEDULER_FEISHU_APP_ID")
                  or _os.getenv("FEISHU_APP_ID") or "")
        app_secret = (_os.getenv("SCHEDULER_FEISHU_APP_SECRET")
                      or _os.getenv("FEISHU_APP_SECRET") or "")
        chat_id = (_os.getenv("SCHEDULER_FEISHU_CHAT_ID")
                   or _os.getenv("FEISHU_CHAT_ID") or "")
        if not (app_id and app_secret):
            try:
                import yaml
                cfg_path = Path.home() / "zhiwei-scheduler" / "config" / "settings.yaml"
                fs = ((yaml.safe_load(cfg_path.read_text()) or {}).get("push", {})
                      .get("feishu", {}) or {})
                app_id = app_id or fs.get("app_id", "")
                app_secret = app_secret or fs.get("app_secret", "")
                chat_id = chat_id or fs.get("chat_id", "")
            except Exception:
                pass
        if not (app_id and app_secret):
            print("⚠️ 图表回传跳过：未找到飞书应用凭据")
            return 0
        pusher = FeishuPusher(app_id=app_id, app_secret=app_secret, chat_id=chat_id)

        for fig in figures:
            source = fig["source"]
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
                out = tf.name
            try:
                p = subprocess.run(
                    [str(rag_python), str(script), "--source", source, "--out", out],
                    capture_output=True, text=True, timeout=60)
                if p.returncode != 0 or not _os.path.exists(out) or _os.path.getsize(out) < 1024:
                    continue
                with open(out, "rb") as f:
                    image_key = pusher.upload_image(f.read())
                if not image_key:
                    continue
                desc = (fig.get("raw_text") or fig.get("text") or "")[:120]
                ctx.reply_message(message_id, f"🖼️ 命中图表：{source}\n{desc}")
                pusher.send_image(image_key)
                sent += 1
            finally:
                try:
                    _os.unlink(out)
                except Exception:
                    pass
    except Exception as e:
        print(f"⚠️ 图表回传跳过（不影响问答）: {e}")
    return sent


def do_knowledge_query(query: str, user_id: str, message_id: str, ctx, deep: bool = False) -> bool:
    """知识库问答：rag_client HTTP 检索 + LLM 回答。deep=True 用扩展检索（研究确认路径）"""
    ctx.reply_message(message_id, "🔍 正在检索知识库...")

    from core.rag_client import get_rag_client
    # 2026-08-13: 单次 HTTP 同时拿回拼接上下文与原始 results（问答沉淀用）
    context, results = get_rag_client().context_with_sources(query, top_k=10 if deep else 5)

    if not context:
        ctx.reply_message(message_id, "💡 知识库中未找到相关直接内容，知微将尝试根据通用知识回答。")
        prompt = query
    else:
        prefix = "【参考资料（扩展检索）】" if deep else "【参考资料】"
        extra = "\n\n要求：综合上述材料给出有组织的要点，注明来源文档，并指出信息缺口。" if deep else ""
        prompt = f"{prefix}\n{context}\n\n问题：{query}{extra}"

    # ⏪ 2026-08-03 用户决策回退：深度研究入口恢复原样，走 Coding Plan
    # （deep=True 为用户主动触发的交互式深度分析，属交互式场景，用 Coding Plan 合规）。
    # 此前曾切 token_plan(preview)、又切 LongCat-2.0，现均不挂；
    # llm.py 里 longcat/token_plan 显式通道保留，待用户重新决定 LongCat 用法。
    # ⭐ 2026-08-14: deep 档升级 extreme 任务（research 角色，prefer_api=token_plan →
    # qwen3.8-max，补测输出 2850字/9.3条洞察是 kimi 的 2 倍；失败自动落 auto 链 kimi-k2.5）。
    # 合规：用户主动点“深入分析”属交互式单次触发，符合 Token Plan 条款。
    # 成本：~3-5次/天 × ~10 Credits ≈ 250/周，占 Standard 10000/周 的 2.5%。
    # 回滚：task 名改回 "deep_analysis" 一行。
    if deep:
        ctx.reply_message(message_id, "📚 检索完成，深度分析中（约 30-90 秒）...")
    task = "extreme" if deep else "context_qa"
    response = llm_client.call_by_task_with_session(task, prompt, f"ask-{user_id}")
    ctx.reply_message(message_id, response)

    # ⭐ F4: 若检索命中图表 chunk，额外回传原图（失败不影响上方回答）
    _send_matched_figures(query, message_id, ctx, top_k=10 if deep else 5)

    # ⭐ 2026-08-13: 问答沉淀一期（失败静默，不影响回答）
    _maybe_append_qa(query, response, results, deep=deep)
    return True


# ============ 问答沉淀（2026-08-13 一期） ============

_QA_APPEND_LOCK = threading.Lock()  # qa_appender 读-改-写无锁，调用侧加锁防并发丢条目
_MAX_ANSWER_CHARS = 1200            # 沉淀答案截断上限


def _maybe_append_qa(query: str, answer: str, results: list, deep: bool = False):
    """把 RAG 命中的问答追加进 top-1 来源笔记的「## 问答参考」区（后台线程）

    一期保守规则：仅浅档（深度研究报告进笔记问答区噪音大）；仅当 top-1
    来源为 .md 且文件存在；任何失败静默跳过，不影响已回答内容。
    """
    try:
        if deep or not results or not answer or str(answer).startswith("❌"):
            return
        top_source = (results[0] or {}).get("source", "")
        if not top_source.endswith(".md"):
            return
        note_path = Path(top_source)
        if not note_path.exists():
            return
        answer_trim = answer[:_MAX_ANSWER_CHARS]
        threading.Thread(
            target=_append_qa_worker,
            args=(note_path, query, answer_trim),
            daemon=True,
        ).start()
    except Exception as e:
        logger.warning(f"问答沉淀跳过: {e}")


def _append_qa_worker(note_path, query, answer):
    try:
        from qa_appender import append_qa
        with _QA_APPEND_LOCK:
            append_qa(str(note_path), query, answer, source="feishu-chat")
    except Exception as e:
        logger.warning(f"问答沉淀失败（不影响回答）: {e}")


# ============ 斜杠命令（行为不变，委托共享函数） ============


def handle_knowledge_commands(text_lower, text_stripped, user_id, message_id, ctx):
    """处理 /ask, /klib, /收录, /insight 等知识库命令"""

    # /insight 灵感捕获 → KarpathyVault
    if text_lower.startswith("/insight ") or text_lower.startswith("/闪念 "):
        content = text_stripped.split(" ", 1)[1] if " " in text_stripped else ""
        if not content:
            ctx.reply_message(message_id, "❌ 请提供灵感内容\n\n用法: /insight 这篇论文的互连拓扑刚好可以解决...")
            return True

        ok, info, filename = do_capture(content, user_id)
        if ok:
            ctx.reply_message(message_id, f"✅ 灵感已捕获\n\n📄 `{filename}`\n等待 Ingest 处理")
        else:
            ctx.reply_message(message_id, f"❌ 写入失败: {info}")
        return True

    # /ask 知识库检索
    if text_lower.startswith("/ask ") or text_lower.startswith("查一下 "):
        query = text_stripped.split(" ", 1)[1] if " " in text_stripped else ""
        if not query:
            ctx.reply_message(message_id, "❌ 请提供查询内容")
            return True
        do_knowledge_query(query, user_id, message_id, ctx)
        return True

    return False
