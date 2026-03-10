"""
命令处理模块
处理所有飞书命令和核心文本消息逻辑
"""

import os
import sqlite3
import json
import subprocess
import tempfile
import time
import re
import threading
import datetime
import sys
from collections import deque
import traceback
from pathlib import Path

# 导入 RAG 桥接
try:
    from rag_bridge import get_context as get_rag_context, is_available as rag_is_available
except ImportError:
    def get_rag_context(query, top_k=5): return ""
    def rag_is_available(): return False

# 添加 scheduler 目录到路径
sys_path_added = False
def _add_scheduler_path():
    global sys_path_added
    if not sys_path_added:
        sys.path.insert(0, os.path.expanduser("~/zhiwei-scheduler"))
        sys_path_added = True

# 导入依赖（由 ws_client.py 初始化）
reply_message = None
reply_card = None
call_openclaw_agent = None
query_knowledge_base = None
get_memory = None
add_to_history = None
get_history = None
is_article_url = None
is_video_url = None
summarize_url = None
handle_video_async = None
extract_video_url = None
TaskLogger = None
detect_chain_intent = None
execute_chain = None
IntentRouter = None
save_active_user = None
load_active_user = None
chat_history = None
pending_voice = None
pending_image = None
pending_review = None
MAX_HISTORY = 20
RATE_LIMIT_SECONDS = 2
user_last_request = None
memory_cache = None

# PDF 解析模块依赖
pdf_parser = None

# 文章写作模块依赖
article_writer = None

# 技术对比模块依赖
tech_compare = None


# ========== ISSUE-029: 异步任务超时兜底 ==========

def run_with_timeout(func, args=(), timeout=300, task_name="任务"):
    """带超时的线程执行器 (ISSUE-029)
    
    Args:
        func: 要执行的函数
        args: 函数参数
        timeout: 超时时间 (秒)
        task_name: 任务名称 (用于错误提示)
    
    Returns:
        (result, error): 成功时 result 为返回值，失败时 error 为错误信息
    """
    result = {"done": False, "error": None, "output": None}
    
    def wrapper():
        try:
            result["output"] = func(*args)
            result["done"] = True
        except Exception as e:
            result["error"] = str(e)
    
    thread = threading.Thread(target=wrapper, daemon=True)
    thread.start()
    thread.join(timeout)
    
    if not result["done"]:
        return None, f"⏰ {task_name}超时（超过 {timeout} 秒），请稍后重试"
    elif result["error"]:
        return None, f"❌ {task_name}失败：{result['error']}"
    return result["output"], None


def init_command_handler(
    global_reply_message, global_reply_card, global_call_openclaw_agent, global_query_knowledge_base,
    global_get_memory, global_add_to_history, global_get_history,
    global_is_article_url, global_is_video_url, global_summarize_url, global_handle_video_async,
    global_extract_video_url, global_TaskLogger, global_detect_chain_intent, global_execute_chain,
    global_IntentRouter, global_save_active_user, global_load_active_user,
    global_chat_history, global_pending_voice, global_pending_image, global_pending_review,
    global_MAX_HISTORY, global_RATE_LIMIT_SECONDS, global_user_last_request, global_memory_cache,
    global_article_writer=None, global_tech_compare=None
):
    """初始化命令处理模块的全局依赖"""
    global reply_message, reply_card, call_openclaw_agent, query_knowledge_base
    global get_memory, add_to_history, get_history
    global is_article_url, is_video_url, summarize_url, handle_video_async, extract_video_url
    global TaskLogger, detect_chain_intent, execute_chain, IntentRouter
    global save_active_user, load_active_user
    global chat_history, pending_voice, pending_image, pending_review
    global MAX_HISTORY, RATE_LIMIT_SECONDS, user_last_request, memory_cache
    global pdf_parser, article_writer, tech_compare

    reply_message = global_reply_message
    reply_card = global_reply_card
    call_openclaw_agent = global_call_openclaw_agent
    query_knowledge_base = global_query_knowledge_base
    get_memory = global_get_memory
    add_to_history = global_add_to_history
    get_history = global_get_history
    is_article_url = global_is_article_url
    is_video_url = global_is_video_url
    summarize_url = global_summarize_url
    handle_video_async = global_handle_video_async
    extract_video_url = global_extract_video_url
    TaskLogger = global_TaskLogger
    detect_chain_intent = global_detect_chain_intent
    execute_chain = global_execute_chain
    IntentRouter = global_IntentRouter
    save_active_user = global_save_active_user
    load_active_user = global_load_active_user
    chat_history = global_chat_history
    pending_voice = global_pending_voice
    pending_image = global_pending_image
    pending_review = global_pending_review
    MAX_HISTORY = global_MAX_HISTORY
    RATE_LIMIT_SECONDS = global_RATE_LIMIT_SECONDS
    user_last_request = global_user_last_request
    memory_cache = global_memory_cache
    article_writer = global_article_writer
    tech_compare = global_tech_compare


# ========== 帮助信息 ==========

def show_help() -> str:
    return """🤖 知微 v2.1 - AI 助手 (RAG增强版 PDF版)

【智能分工】
📝 普通对话 → 知微直接回答
📚 知识库 → 自动触发或用 /ask
🔍 研究分析 → 自动转给探微
💻 编程开发 → 自动转给筑微
📢 推送运营 → 自动转给通微

【新增命令】
/dev <需求> - 提交开发任务（修改代码/配置）
/收录 <URL> - 收录网页到知识库
/ask <问题> - 强制查询本地知识库
/pdf <file_key> - 解析飞书PDF文档（异步）
/写稿 <主题> - 撰写文章（自动检索 + AI生成）
/对比 A vs B - 技术对比（生成对比表格）
/status - 查看知识库收录状态（含向量化进度）
回复「好」或「不要」 - 审批开发任务

【性能优化】
⚡ RAG 检索延迟已优化，响应更快

【原有支持】
📝 文字 - AI 多轮对话（带记忆）
🖼️ 图片 - 图片分析 + 追问
🌐 网页链接 - 自动抓取总结
🎬 视频链接 - 抖音/YouTube/B站

💡 直接发消息，我会自动判断是否查资料！"""


# ========== 核心：文本消息处理 ==========

def handle_text_async(text: str, user_id: str, message_id: str):
    """异步处理文本消息 - 带记忆、路由和RAG"""
    try:
        text_stripped = text.strip()
        text_lower = text_stripped.lower()

        # ===== 0. 审批确认流程 (T-056) =====
        if user_id in pending_review:
            task_id = pending_review[user_id]

            if text_lower in ["好", "可以", "执行", "ok", "yes", "同意", "批准", "行", "执行吧", "没问题", "approve"]:
                # 添加项目路径
                if os.path.expanduser("~/zhiwei-dev") not in sys.path:
                    sys.path.insert(0, os.path.expanduser("~/zhiwei-dev"))
                from task_store import TaskStore
                store = TaskStore()
                
                if store.approve(task_id):
                    del pending_review[user_id]
                    reply_message(message_id, f"✅ 任务 #{task_id} 已批准，开始执行...\n\n完成后会推送结果。")
                else:
                    del pending_review[user_id]
                    reply_message(message_id, f"⚠️ 任务 #{task_id} 审批失败 (可能已被取消或已执行)")
                return

            elif text_lower in ["不要", "取消", "不", "no", "拒绝", "算了", "不行", "reject"]:
                if os.path.expanduser("~/zhiwei-dev") not in sys.path:
                    sys.path.insert(0, os.path.expanduser("~/zhiwei-dev"))
                from task_store import TaskStore
                store = TaskStore()
                
                if store.reject(task_id):
                    reply_message(message_id, f"❌ 任务 #{task_id} 已取消")
                else:
                    reply_message(message_id, f"⚠️ 任务 #{task_id} 取消失败 (可能状态已变)")
                    
                del pending_review[user_id]
                return

            # 不是审批回复，保留状态，继续正常处理

        # ===== 1. 语音确认流程 =====
        if user_id in pending_voice:
            pending_text = pending_voice.pop(user_id)
            if text_lower in ["取消", "cancel", "算了"]:
                reply_message(message_id, "✅ 已取消")
                return
            if text_lower in ["ok", "确认", "好的", "执行", "是", "yes"]:
                text_stripped = pending_text
            # 否则使用用户修改后的内容

        # ===== 2. 图片追问 =====
        if user_id in pending_image:
            img_data = pending_image[user_id]
            if isinstance(img_data, dict) and "base64" in img_data:
                if time.time() - img_data.get("time", 0) < 600:
                    # 不是命令才走图片追问
                    if not text_lower.startswith("/") and not (len(text_lower) == 2 and text_lower[0] == 'm'):
                        print(f"🖼️ 图片追问: {text_stripped[:30]}...")
                        response = analyze_image_base64(img_data["base64"], text_stripped)
                        reply_message(message_id, response + "\n\n💡 继续追问或发新图片")
                        return
                else:
                    del pending_image[user_id]

        # ===== 3. 命令处理 =====

        if text_lower.startswith("/help") or text_lower in ["帮助", "/帮助"] or text_lower == "help":
            reply_message(message_id, show_help())
            return

        # 开发任务显式触发 (T-052: 绕过 OpenClaw LLM 路由)
        # 新版开发系统集成 (zhiwei-dev)
        if text_lower.startswith("/dev ") or text_lower.startswith("@开发 "):
            requirement = text_stripped.split(" ", 1)[1] if " " in text_stripped else ""
            if not requirement:
                reply_message(message_id, "❌ 请提供需求描述\n\n用法: /dev 把早报时间改成8点30分")
                return

            sys.path.insert(0, os.path.expanduser("~/zhiwei-dev"))

            try:
                from task_store import TaskStore
                store = TaskStore()
                # 根据风险评估决定是否进入审批流 (v32.4 优化)
                from worker import Worker
                w = Worker()
                risk_level = w._assess_risk(requirement)
                
                # 如果是 auto 或者是来自 scheduler 的计划，直接 pending 执行
                initial_status = "pending" if risk_level == "auto" else "review"
                
                task_id = store.enqueue(requirement, message_id=message_id, initial_status=initial_status)

                # 获取当天序号
                daily_seq = store.get_daily_seq(task_id)

                # 记录 user_id 到文件，供后续通知使用（新版 MessageBus 也会读取）
                user_mappings_dir = os.path.expanduser("~/zhiwei-dev/user_mappings")
                os.makedirs(user_mappings_dir, exist_ok=True)
                
                user_file = os.path.join(user_mappings_dir, f"task_{task_id}_user.json")
                with open(user_file, "w") as f:
                    json.dump({
                        "user_id": user_id, 
                        "message_id": message_id,
                        "source": "feishu",
                        "via": "message_bus_v2"
                    }, f)

                # 将路由信息也尝试存入 Task 存储（如果 TaskStore 支持 metadata 字段，目前逻辑主要在 enqueue 后的上下文）

                # 如果需要审批
                if initial_status == "review":
                    # 记录待审批状态在内存，等待用户确认
                    pending_review[user_id] = task_id
                    
                    risk_msg = "⚠️ 包含敏感操作或受保护文件" if risk_level == "approve" else "ℹ️ 需确认执行范围"
                    
                    reply_message(
                        message_id,
                        f"👋 收到开发需求\n\n"
                        f"📌 {requirement}\n\n"
                        f"🕒 风险判定: {risk_level} ({risk_msg})\n"
                        f"👉 请回复「批准」执行，或回复「取消」放弃"
                    )
                else:
                    reply_message(
                        message_id,
                        f"👋 收到开发需求\n"
                        f"✅ 风险判定[安全]，直接加入队列\n\n"
                        f"任务 日常#{daily_seq} 已排队，请耐心等待~"
                    )
                    
            except Exception as e:
                traceback.print_exc()
                reply_message(message_id, f"❌ 投递任务失败: {e}")
            return

        # Phase 4 新增: RAG 强制查询
        if text_lower.startswith("/ask ") or text_lower.startswith("查 "):
            query = text_stripped.split(" ", 1)[1]
            reply_message(message_id, f"🔍 正在检索知识库：{query}...")

            # 优先使用新 RAG
            if rag_is_available():
                rag_result = get_rag_context(query, top_k=5)
                if rag_result:
                    reply_message(message_id, f"🚀 **zhiwei-rag (三轨精排) 结果**\n\n{rag_result}")
                    return

            # 降级到旧方案
            rag_result = query_knowledge_base(query)
            if rag_result:
                reply_message(message_id, f"📚 **知识库结果 (旧)**\n\n{rag_result}")
            else:
                reply_message(message_id, "❌ 知识库中未找到相关内容")
            return

        # PDF 文档解析 (knowledge-collect 子功能)
        if text_lower.startswith("/pdf ") or text_lower.startswith("解析PDF "):
            file_key = text_stripped.split(" ", 1)[1] if " " in text_stripped else ""
            if not file_key:
                reply_message(message_id, "❌ 请提供 PDF 文件的 file_key\n\n用法: /pdf <file_key>")
                return

            reply_message(message_id, f"📄 正在解析 PDF 文档...\n\n⏳ 请稍候，提取内容可能需要几分钟")

            # 异步调用 PDF 解析模块
            def _process_pdf_task():
                try:
                    _add_scheduler_path()
                    # 动态导入 pdf_parser 模块
                    import pdf_parser as pdf_mod

                    # 检查模块是否已初始化
                    if pdf_mod.client is None:
                        pdf_mod.init_pdf_parser(client, reply_message, TaskLogger, time)

                    pdf_mod.handle_pdf_async(message_id, file_key, user_id)
                except Exception as e:
                    traceback.print_exc()
                    reply_message(message_id, f"❌ PDF 解析异常: {str(e)}")

            thread = threading.Thread(target=_process_pdf_task, daemon=True)
            thread.start()
            return

        # T-069: /写稿 命令 - 生成文章 (ISSUE-029: 添加超时兜底)
        if text_lower.startswith("/写稿 ") or text_lower.startswith("写稿 "):
            topic = text_stripped.split(" ", 1)[1] if " " in text_stripped else ""
            if not topic:
                reply_message(message_id, "❌ 请提供文章主题\n\n用法：/写稿 AI Agent 技术趋势")
                return

            # 发送确认消息，告知已开始生成
            reply_message(message_id, f"✅ /写稿 任务已接收，主题：「{topic}」\n\n📝 文章生成中，请稍候...\n⏰ 预计 2-3 分钟，完成后将主动推送给您")

            # 传递用户 ID，用于后续主动推送结果
            user_id_for_callback = user_id

            # 异步调用文章写作模块 (ISSUE-029: 使用 run_with_timeout 包装)
            def _process_article_task():
                try:
                    _add_scheduler_path()
                    # 动态导入 article_writer 模块
                    import article_writer as aw

                    # 调用修改后的 write_article，传入用户 ID 用于回调
                    article = aw.write_article(topic, user_id_for_callback)

                    # 不再使用 reply_message，因为是在后台线程中执行
                    # 结果将由 article_writer 通过 send_direct_message 主动推送
                except Exception as e:
                    print(f"❌ 文章写作异常：{e}")
                    traceback.print_exc()
                    # 发送错误信息给用户
                    from feishu_api import send_direct_message
                    send_direct_message(user_id_for_callback, f"❌ 文章生成失败：{str(e)}")

            # ISSUE-029: 使用超时包装，180 秒超时
            result, error = run_with_timeout(_process_article_task, timeout=180, task_name="写稿")
            if error:
                from feishu_api import send_direct_message
                send_direct_message(user_id_for_callback, error)
            return

        # T-071: /对比 命令 - 技术对比 (ISSUE-029: 添加超时兜底)
        if text_lower.startswith("/对比 ") or text_lower.startswith("对比 "):
            comparison_text = text_stripped.split(" ", 1)[1] if " " in text_stripped else ""
            if not comparison_text:
                reply_message(message_id, "❌ 请提供对比项\n\n用法：/对比 React vs Vue")
                reply_message(message_id, "支持格式：/对比 A vs B / 对比 A vs B / 对比 A 与 B")
                return

            # 立即回复用户，告知已开始生成
            reply_message(message_id, f"📊 已开始生成「{comparison_text}」对比\n\n⏳ 预计 2-3 分钟，完成后将主动推送结果")

            # 启动后台线程处理对比任务 (ISSUE-029: 使用 run_with_timeout 包装)
            def _process_compare_task():
                try:
                    _add_scheduler_path()
                    # 动态导入 tech_compare 模块
                    import tech_compare as tc

                    # 先解析对比项
                    item_a, item_b = tc.parse_comparison(comparison_text)
                    if not item_b:
                        reply_message(message_id, "❌ 无法解析对比项，请使用 'A vs B' 格式")
                        return

                    # 调用对比函数并传入用户 ID 以便主动推送结果
                    result = tc.compare_tech(item_a, item_b, user_id)

                    # 如果推送失败，可以提供一个备用的反馈
                    print(f"📊 技术对比任务完成，结果已推送给用户 {user_id}")
                except Exception as e:
                    print(f"❌ 技术对比异常：{e}")
                    traceback.print_exc()
                    # 尝试推送错误信息给用户
                    try:
                        from feishu_api import send_direct_message
                        send_direct_message(user_id, f"❌ 对比生成失败：{str(e)}")
                    except:
                        reply_message(message_id, f"❌ 对比生成失败：{str(e)}")

            # ISSUE-029: 使用超时包装，90 秒超时
            result, error = run_with_timeout(_process_compare_task, timeout=90, task_name="对比")
            if error:
                from feishu_api import send_direct_message
                send_direct_message(user_id, error)
            return

        # T-065: 收录网页到知识库 — 调用 knowledge-collect Skill
        if text_lower.startswith("/收录 ") or text_lower.startswith("收录 ") or text_lower.startswith("收录:"):
            # 提取 URL 和可选标签
            url = text_stripped.split(" ", 1)[1] if " " in text_stripped else ""
            url = url.split(":", 1)[1] if ":" in url and not url.startswith("http") else url
            url = url.strip()

            # 解析标签（如果有）
            url_and_tags = url.split(maxsplit=1)
            url = url_and_tags[0].strip()
            tags = url_and_tags[1].strip() if len(url_and_tags) > 1 else ""

            if not url:
                reply_message(message_id, "❌ 请提供要收录的 URL\n\n用法: /收录 https://example.com/article")
                return

            # 验证 URL 格式
            if not url.startswith(("http://", "https://")):
                reply_message(message_id, "❌ URL 格式不正确，需要以 http:// 或 https:// 开头")
                return

            reply_message(message_id, f"📥 正在收录: {url}")

            # 同步调用 knowledge-collect Skill
            try:
                cmd = f'docker exec clawdbot python3 /root/workspace/skills/knowledge-collect/collect.py --url "{url}"'
                if tags:
                    cmd += f' --tags "{tags}"'
                result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)

                if result.returncode == 0:
                    try:
                        data = json.loads(result.stdout.strip())
                        if data.get("status") == "ok":
                            # 获取收录成功的标题，用于创建 Obsidian 笔记
                            title = data.get('title', '未知名称')

                            # 在后台线程中创建 Obsidian 笔记
                            def _create_obsidian_note():
                                try:
                                    # 将特殊字符替换为适合文件名的形式
                                    import re
                                    safe_title = re.sub(r'[<>:"/\\|?*]', '_', title)
                                    safe_title = safe_title[:50]  # 限制长度

                                    # 构建 Obsidian 笔记内容
                                    note_content = f"""# {title}

URL: {url}

## 摘要
{data.get('summary', '无摘要')}

## 标签
{tags if tags else '无'}

## 收录时间
{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""

                                    # 调用 Obsidian CLI 创建笔记
                                    obs_cmd = f'/opt/homebrew/bin/obsidian-cli create "Inbox/{safe_title}.md" --vault="~/Documents/ZhiweiVault" --content="{note_content.replace(chr(10), "\\n").replace(chr(9), "\\t")}"'
                                    obs_result = subprocess.run(obs_cmd, shell=True, capture_output=True, text=True, timeout=30)

                                    if obs_result.returncode == 0:
                                        print(f"✅ Obsidian 笔记创建成功: {safe_title}")

                                        # 调用自动链接功能（异步处理，避免阻塞主线程）
                                        def _link_related_notes():
                                            try:
                                                from obsidian_linker import link_new_note
                                                import time
                                                # 等待文件创建完成
                                                time.sleep(2)
                                                note_path = Path.home() / "Documents" / "ZhiweiVault" / f"Inbox/{safe_title}.md"
                                                link_new_note(note_path, note_content)
                                            except ImportError:
                                                print("⚠️ 未能导入 obsidian_linker，跳过自动链接")
                                            except Exception as e:
                                                print(f"❌ 自动链接过程中出错: {e}")

                                        # 在后台线程中处理链接
                                        link_thread = threading.Thread(target=_link_related_notes, daemon=True)
                                        link_thread.start()
                                    else:
                                        print(f"❌ Obsidian 笔记创建失败: {obs_result.stderr}")

                                except Exception as e:
                                    print(f"❌ 创建 Obsidian 笔记时出错: {e}")

                            # 启动后台线程创建 Obsidian 笔记
                            obs_thread = threading.Thread(target=_create_obsidian_note, daemon=True)
                            obs_thread.start()

                            reply_message(message_id,
                                f"✅ 已收录到知识库\n"
                                f"📄 标题: {data.get('title', '未知')}\n"
                                f"📏 字数: {data.get('word_count', 0)}\n"
                                f"📁 位置: knowledge-inbox/unsorted/\n"
                                f"📘 同步创建 Obsidian 笔记中...")
                        else:
                            reply_message(message_id, f"❌ 收录失败: {data.get('message', '未知错误')}")
                    except json.JSONDecodeError:
                        reply_message(message_id, f"⚠️ 收录完成但返回格式异常:\n{result.stdout[:200]}")
                else:
                    reply_message(message_id, f"❌ 收录失败:\n{result.stderr[:200]}")
            except subprocess.TimeoutExpired:
                reply_message(message_id, "❌ 收录超时(60s)，该网页可能需要JS渲染")
            except Exception as e:
                reply_message(message_id, f"❌ 收录异常: {str(e)}")
            return

        if text_lower.startswith("/reset") or text_lower in ["重置", "/重置", "新对话"]:
            memory = get_memory(user_id)
            memory.reset()
            if user_id in pending_image:
                del pending_image[user_id]
            reply_message(message_id, "✅ 会话和记忆已重置\n\n开始新对话吧！")
            return

        if text_lower.startswith("/history") or text_lower in ["历史", "/记录"]:
            reply_message(message_id, get_history(user_id))
            return

        if text_lower.startswith("/memory") or text_lower in ["记忆", "/记忆"]:
            memory = get_memory(user_id)
            reply_message(message_id, f"🧠 记忆状态\n\n{memory.get_stats()}")
            return

        if text_lower.startswith("/tasks") or text_lower in ["任务", "/任务"]:
            reply_message(message_id, TaskLogger.get_recent(5))
            return

        if text_lower.startswith("/route "):
            test_msg = text_stripped[7:]
            result = IntentRouter.explain(test_msg)
            reply_message(message_id, f"🔀 路由测试\n\n{result}")
            return

        if text_lower in ["/sync", "同步", "/session", "会话"]:
            session_id = get_session_id(user_id)
            reply_message(message_id, f"📌 会话 ID: {session_id}")
            return

        if text_lower.startswith("/status") or text_lower in ["状态", "/状态"]:
            reply_message(message_id, get_quick_status())
            return

        if text_lower.startswith("/model") or text_lower in ["模型", "/模型"]:
            try:
                config_path = os.path.expanduser("~/logs/current_model.json")
                if os.path.exists(config_path):
                    with open(config_path) as f:
                        data = json.load(f)
                    msg = f"""🤖 当前模型

**{data.get('name', '未知')}**
• 模型ID: {data.get('model', '未知')}
• Provider: {data.get('provider', '未知')}

切换: m1-m8"""
                    reply_message(message_id, msg)
                else:
                    reply_message(message_id, "❌ 模型配置未找到")
            except Exception as e:
                reply_message(message_id, f"❌ 获取模型失败: {e}")
            return

        # 模型切换
        if len(text_lower) == 2 and text_lower[0] == 'm' and text_lower[1] in "12345678":
            try:
                result = subprocess.run(
                    ['/usr/local/bin/ocmodel', text_lower[1]],
                    capture_output=True, text=True, timeout=5
                )
                msg = f"✅ {result.stdout.strip()}" if result.returncode == 0 else f"❌ {result.stderr}"
                reply_message(message_id, msg)
            except Exception as e:
                reply_message(message_id, f"❌ 切换异常: {e}")
            return

        # 兜底：拦截所有未识别的 / 命令，防止漏给 OpenClaw
        if text_stripped.startswith("/"):
            reply_message(message_id, f"❌ 未知命令: `{text_stripped.split()[0]}`\n\n发送 /help 查看所有可用命令。")
            return

        # ===== 4. 网页链接 =====
        if is_article_url(text_stripped):
            url = extract_article_url(text_stripped)
            reply_message(message_id, "🌐 正在抓取网页，请稍候...")
            summary_prompt = summarize_url(url)
            if summary_prompt.startswith("❌"):
                reply_message(message_id, summary_prompt)
                return
            session_id = get_session_id(user_id)
            response = call_openclaw_agent(summary_prompt, session_id)
            reply_card(message_id, "🌐 网页总结", response)
            TaskLogger.log_task("网页总结", "完成", url)
            return

        # ===== 5. 视频链接 =====
        if is_video_url(text_stripped):
            reply_message(message_id, "🎬 开始分析视频...\n\n⏳ 预计需要3-5分钟，完成后自动回复")
            handle_video_async(text_stripped, message_id, user_id)
            return

        # ===== 6. 核心：带记忆的 Agent 对话 =====

        # 6a. 记录到轻量历史
        add_to_history(user_id, "user", text_stripped)

        # 6b. 获取记忆上下文
        memory = get_memory(user_id)
        context_prompt = memory.build_context_prompt()

        # 6b-2. 协作链检测
        chain_name = detect_chain_intent(text_stripped)
        if chain_name:
            reply_message(message_id, f"🔗 检测到协作链任务，开始执行...")
            try:
                session_id = get_session_id(user_id)
                chain_response = execute_chain(chain_name, text_stripped, session_id)
                reply_message(message_id, chain_response)
                TaskLogger.log_task("协作链", chain_name, text_stripped[:50])
                return
            except Exception as e:
                print(f"❌ 协作链异常: {e}")
                reply_message(message_id, f"❌ 协作链执行失败: {e}")
                return

        # Phase 4 新增: 智能 RAG 触发 (检测关键词)
        # 如果问题中包含明显查资料意图，或者涉及知识库关键词，自动补充 RAG 结果
        rag_context = ""
        rag_triggers = ["查一下", "搜一下", "知识库", "库里", "文档", "书中", "书里"]
        if any(keyword in text_stripped for keyword in rag_triggers):
            reply_message(message_id, "🔍 正在自动检索知识库...")
            rag_result = query_knowledge_base(text_stripped)
            if rag_result:
                rag_context = f"\n\n【本地知识库参考资料】\n{rag_result}\n"
                print("✅ 智能 RAG 触发成功")

        # 6c. 意图路由
        target_agent = IntentRouter.route(text_stripped)

        # 6d. 构建增强消息 (上下文 + RAG + 问题)
        enriched_message = ""
        if context_prompt:
            enriched_message += f"{context_prompt}\n\n"
        if rag_context:
            enriched_message += f"{rag_context}\n\n"

        enriched_message += f"---\n当前问题: {text_stripped}"

        # 如果有 RAG 内容，强制提示 Agent 使用
        if rag_context:
            enriched_message += "\n(请结合参考资料回答)"

        # 6e. 调用 Agent
        session_id = get_session_id(user_id)
        response = call_openclaw_agent(enriched_message, session_id, agent=target_agent)

        # 6f. 如果路由到其他Agent，加标注
        if target_agent != "main":
            agent_names = {
                "researcher": "探微",
                "developer": "筑微",
                "operator": "通微"
            }
            agent_label = agent_names.get(target_agent, target_agent)
            response = f"🔀 *已转交{agent_label}处理*\n\n{response}"

        # 6g. 更新记忆
        memory.add_turn(text_stripped, response)

        # 自动提取重要信息到持久记忆
        try:
            from memory_manager import extract_important_info
            important = extract_important_info(text_stripped, response)
            if important:
                memory.save_persistent(important["key"], important["value"])
                print(f"💾 自动保存: {important['key']}")
        except Exception as e:
            pass  # 静默失败

        # 6h. 记录到轻量历史
        add_to_history(user_id, "bot", response)

        # 6i. 回复
        reply_message(message_id, response)

    except Exception as e:
        print(f"❌ 文本处理异常: {e}")
        traceback.print_exc()
        reply_message(message_id, f"❌ 处理异常，请重试")


# ========== 工具函数 ==========

def get_session_id(user_id: str) -> str:
    return f"feishu-{user_id}"


def get_quick_status() -> str:
    lines = ["📊 系统状态 (v2.1 RAG版)\n"]

    # Docker
    try:
        result = subprocess.run(
            "docker ps --format '{{.Names}}: {{.Status}}' | head -3",
            shell=True, capture_output=True, text=True, timeout=5
        )
        lines.append("**容器:**")
        for line in result.stdout.strip().split('\n')[:3]:
            if line:
                lines.append(f"  • {line}")
    except:
        lines.append("  • 容器状态获取失败")

    # 模型
    try:
        config_path = os.path.expanduser("~/logs/current_model.json")
        if os.path.exists(config_path):
            with open(config_path) as f:
                data = json.load(f)
                lines.append(f"\n**模型:** {data.get('name', '未知')}")
    except:
        pass

    # 知识库状态 (查询 Core/Important/Reference 进度)
    try:
        db_path = os.path.expanduser("~/Documents/Library/klib.db")
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT priority, COUNT(*) as total, "
            "SUM(CASE WHEN vectorized = 1 THEN 1 ELSE 0 END) as done "
            "FROM books GROUP BY priority"
        ).fetchall()
        conn.close()
        
        lines.append("\n**📊 向量化进度:**")
        priority_order = ['core', 'important', 'reference']
        progress = {row[0].lower(): (row[2], row[1]) for row in rows if row[0]}
        for p in priority_order:
            done, total = progress.get(p, (0, 0))
            pct = (done / total * 100) if total > 0 else 0
            emoji = "🔴" if p == "core" else "🟡" if p == "important" else "⚪"
            lines.append(f"  {emoji} {p.capitalize()}: {pct:.0f}% ({done}/{total})")
    except Exception as e:
        lines.append(f"  • 向量化进度查询失败: {e}")

    return "\n".join(lines)


def check_rate_limit(user_id: str) -> bool:
    # user_last_request 在 init_command_handler 中初始化为 defaultdict(float)
    global user_last_request
    if user_last_request is None:
        return True  # 未初始化时跳过限流
    now = time.time()
    last = user_last_request[user_id]
    if now - last < RATE_LIMIT_SECONDS:
        return False
    user_last_request[user_id] = now
    return True
