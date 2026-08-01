from zhiwei_common.llm import llm_client
import time
from pathlib import Path

# ============ 共享函数（NL 路由器与斜杠命令共用，2026-07-26 P1） ============


def do_capture(content: str, user_id: str, source: str = "飞书 /insight") -> tuple:
    """把灵感/知识片段写入 KarpathyVault/_raw/

    Returns: (success, filepath_or_error, filename)
    """
    vault_path = Path.home() / "KarpathyVault" / "_raw"
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
    context = get_rag_client().get_context(query, top_k=10 if deep else 5)

    if not context:
        ctx.reply_message(message_id, "💡 知识库中未找到相关直接内容，知微将尝试根据通用知识回答。")
        prompt = query
    else:
        prefix = "【参考资料（扩展检索）】" if deep else "【参考资料】"
        extra = "\n\n要求：综合上述材料给出有组织的要点，注明来源文档，并指出信息缺口。" if deep else ""
        prompt = f"{prefix}\n{context}\n\n问题：{query}{extra}"

    response = llm_client.call_with_session("chat", prompt, f"ask-{user_id}")
    ctx.reply_message(message_id, response)

    # ⭐ F4: 若检索命中图表 chunk，额外回传原图（失败不影响上方回答）
    _send_matched_figures(query, message_id, ctx, top_k=10 if deep else 5)
    return True


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
