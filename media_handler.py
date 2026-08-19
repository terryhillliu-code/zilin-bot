"""
媒体处理模块
处理图片、视频、语音和URL相关功能
"""

import os
import re
import tempfile
import base64
import threading
import subprocess
import shutil
import logging
import sys
from pathlib import Path
from typing import Optional

# 导入统一的 API Key 获取函数
try:
    from zhiwei_common import get_api_key
except ImportError:
    from zhiwei_common import get_api_key

# 引入 distiller 以便复用 ASR 逻辑 (v6.0; v3.3 增 mimo-asr + MLX 本地)
try:
    from scripts.douyin_distiller import (
        DashScopeASRTranscriber, MimoASRTranscriber,
        LocalMLXWhisperTranscriber, AppConfig,
    )
except ImportError:
    DashScopeASRTranscriber = None
    MimoASRTranscriber = None
    LocalMLXWhisperTranscriber = None
    AppConfig = None

# 引入 Mimo TTS 客户端
try:
    from mimo_tts import MimoTTSClient
except ImportError:
    MimoTTSClient = None

# 设置日志
logger = logging.getLogger(__name__)

# ⭐ v70.6: 任务账本（蒸馏任务全程留痕，中断可断点续跑）
import task_journal

# 导入依赖（由 ws_client.py 初始化）
client = None
reply_message = None
TaskLogger = None
pending_image = None
pending_voice = None
time = None


def init_media_handler(global_client, global_reply_message, global_task_logger, global_pending_image, global_pending_voice, global_time):
    """初始化媒体处理模块的全局依赖"""
    global client, reply_message, TaskLogger, pending_image, pending_voice, time
    client = global_client
    reply_message = global_reply_message
    TaskLogger = global_task_logger
    pending_image = global_pending_image
    pending_voice = global_pending_voice
    time = global_time


# ========== 图片处理 ==========

def download_image(message_id: str, image_key: str) -> str:
    """下载飞书图片"""
    try:
        from lark_oapi.api.im.v1 import GetMessageResourceRequest
        tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
        tmp_path = tmp_file.name
        tmp_file.close()

        request = GetMessageResourceRequest.builder() \
            .message_id(message_id) \
            .file_key(image_key) \
            .type("image") \
            .build()
        response = client.im.v1.message_resource.get(request)

        if response.success():
            with open(tmp_path, "wb") as f:
                f.write(response.file.read())
            size = os.path.getsize(tmp_path)
            print(f"✅ 图片下载成功: {tmp_path} ({size} bytes)")
            return tmp_path
        else:
            print(f"❌ 图片下载失败: {response.code} - {response.msg}")
            return None
    except Exception as e:
        print(f"❌ 图片下载异常: {e}")
        return None


def compress_image_base64(image_path: str, max_size: int = 800) -> str:
    """压缩图片为 base64"""
    try:
        from PIL import Image
        import io

        with Image.open(image_path) as img:
            if img.mode in ('RGBA', 'P'):
                img = img.convert('RGB')

            ratio = min(max_size / img.width, max_size / img.height)
            if ratio < 1:
                new_size = (int(img.width * ratio), int(img.height * ratio))
                img = img.resize(new_size, Image.Resampling.LANCZOS)

            buffer = io.BytesIO()
            img.save(buffer, format='JPEG', quality=85)
            compressed = buffer.getvalue()

            print(f"🖼️ 图片压缩: {os.path.getsize(image_path)} → {len(compressed)} bytes")
            return base64.b64encode(compressed).decode()
    except ImportError:
        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode()
    except Exception as e:
        print(f"⚠️ 压缩失败，使用原图: {e}")
        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode()


def analyze_image_base64(image_base64: str, question: str = None) -> str:
    """调用统一多模态出口分析图片

    2026-07-31 修复：旧实现构建了 messages（含 image_url）却从未使用，
    实际只把文本 prompt 传给 client.call("vision", ...)——图片从没进过模型，
    所谓"图片分析"实为凭文本瞎编。现改调 zhiwei_common.llm.call_vision，
    真正将图片送入模型（已实测验证）。
    """
    try:
        from zhiwei_common.llm import call_vision

        prompt = question if question else "请分析这张图片，描述内容并提取关键信息。"
        print(f"🖼️ 调用多模态模型分析图片... 问题: {prompt[:30]}...")

        success, content = call_vision(
            prompt, image_b64=image_base64, image_mime="image/jpeg", max_tokens=2000
        )

        if success:
            print(f"✅ 图片分析完成: {len(content)} 字符")
            return f"🖼️ **图片分析结果**\n\n{content}"
        print(f"❌ 图片分析失败: {content}")
        return f"❌ 图片分析失败: {content}"
    except Exception as e:
        print(f"❌ 图片分析异常: {e}")
        return f"❌ 图片分析异常: {str(e)}"


def handle_image_async(message_id: str, image_key: str, user_id: str):
    """异步处理图片"""
    try:
        image_path = download_image(message_id, image_key)
        if not image_path:
            reply_message(message_id, "❌ 图片下载失败，请重试")
            return

        image_base64 = compress_image_base64(image_path)

        pending_image[user_id] = {
            "base64": image_base64,
            "time": time.time()
        }

        response = analyze_image_base64(image_base64)

        if os.path.exists(image_path):
            os.remove(image_path)

        reply_message(message_id, response + "\n\n💡 你可以继续针对这张图片提问")
    except Exception as e:
        print(f"❌ 图片处理异常: {e}")
        reply_message(message_id, f"❌ 图片处理异常: {str(e)}")


# ========== 视频链接 ==========


def _extract_douyin_share_id(text: str) -> str | None:
    """从抖音 App 分享文本中提取视频 ID 编码（无 URL 时的降级策略）

    抖音 App 新版分享格式（2024 年起）：
    '6.43 复制打开抖音，看看【...】 06/30 vFH:/'
    末尾的 'vFH' 是视频 ID 编码，需要拼成短链 https://v.douyin.com/vFH/

    前置条件：文本必须包含抖音特征词，避免误伤非抖音内容

    返回：拼接后的 URL 字符串，或 None
    """
    # 前置条件：文本必须包含抖音特征词，避免误伤
    if not any(kw in text for kw in ('复制打开抖音', '抖音', 'douyin')):
        return None

    # 提取末尾的 ID 编码（3-20 位字母数字下划线，可带 :/ 后缀）
    # 例：'06/30 vFH:/' → 'vFH'
    #      'abc123DEF:/' → 'abc123DEF'
    m = re.search(r'\b([A-Za-z0-9_-]{3,20}):?/?\s*$', text.strip())
    if not m:
        return None

    candidate = m.group(1)

    # 过滤明显误匹配的 ID（如纯数字、常见英文单词）
    if candidate.isdigit():
        return None
    # 过滤过短的 ID（抖音 ID 编码通常 >= 3 位）
    if len(candidate) < 3:
        return None

    # 拼接为抖音短链（下游 douyin_distiller 会解析）
    return f'https://v.douyin.com/{candidate}/'


def extract_video_url(text: str) -> str:
    """提取视频 URL

    支持格式：
    - https://v.douyin.com/xxx/ (短链)
    - https://www.douyin.com/video/xxx (长链)
    - douyin.com/video/xxx (无协议前缀，抖音新分享格式)
    - https://douyin.com/video/xxx (无 www)
    - B站、YouTube 等
    - ⭐ 2026-08-08 P4: 小红书/快手/微博/TikTok/X 全平台放行
      （下载由 distiller 内 yt-dlp 承接，解析失败有明确错误回复）
    """
    patterns = [
        # 抖音短链
        r'(https?://v\.douyin\.com/[A-Za-z0-9_-]+/?)',
        # ⭐ 2026-08-05: 抖音新版分享格式 iesdouyin.com/share/video/ID（用户实测漏识别）
        r'(https?://(?:www\.)?iesdouyin\.com/share/video/\d+)',
        # 抖音长链（带 www）
        r'(https?://www\.douyin\.com/video/\d+)',
        # 抖音长链（无 www，新版分享格式）
        r'(https?://douyin\.com/video/\d+)',
        # 抖音长链（无协议前缀，需要补全）
        r'(?<![\w./])(douyin\.com/video/\d+)',
        # YouTube（watch/短链/Shorts 2026-08-02 补）
        r'(https?://(?:www\.)?youtube\.com/watch\?v=[A-Za-z0-9_-]+)',
        r'(https?://youtu\.be/[A-Za-z0-9_-]+)',
        r'(https?://(?:www\.)?youtube\.com/shorts/[A-Za-z0-9_-]+)',
        # B站
        r'(https?://(?:www\.)?bilibili\.com/video/[A-Za-z0-9_-]+)',
        r'(https?://b23\.tv/[A-Za-z0-9_-]+)',
        # ⭐ 2026-08-08 P4: 小红书（explore/discovery 页 + xhslink 短链）
        r'(https?://(?:www\.)?xiaohongshu\.com/[^\s<>"]+)',
        r'(https?://xhslink\.com/[A-Za-z0-9/_-]+)',
        # 快手（short-video 页 + v.kuaishou 短链）
        r'(https?://(?:www\.)?kuaishou\.com/[^\s<>"]+)',
        r'(https?://v\.kuaishou\.com/[A-Za-z0-9_-]+)',
        # 微博视频页（tv/show 与状态页，含 m. 等子域）
        r'(https?://(?:[A-Za-z0-9-]+\.)*weibo\.com/tv/show/[^\s<>"]+)',
        r'(https?://(?:[A-Za-z0-9-]+\.)*weibo\.com/\d+/[A-Za-z0-9]+[^\s<>"]*)',
        r'(https?://(?:[A-Za-z0-9-]+\.)*weibo\.cn/[^\s<>"]+)',
        # TikTok（视频页 + vm 短链）
        r'(https?://(?:www\.)?tiktok\.com/@[^\s<>"]+/video/\d+)',
        r'(https?://vm\.tiktok\.com/[A-Za-z0-9_-]+)',
        # X / Twitter 状态页（含视频）
        r'(https?://(?:www\.)?(?:x|twitter)\.com/\w+/status/\d+[^\s<>"]*)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            url = match.group(1)
            # 补全协议前缀
            if url.startswith('douyin.com'):
                url = 'https://' + url
            # ⭐ 2026-08-05: iesdouyin 分享链接归一化为标准格式
            # （下游 douyin-api :8680 只认 douyin.com/video/ID，不认 share/video）
            _m = re.match(r'https?://(?:www\.)?iesdouyin\.com/share/video/(\d+)', url)
            if _m:
                url = f'https://www.douyin.com/video/{_m.group(1)}'
            return url
    # ⭐ 2026-08-17: 抖音分享文本无 URL 时，降级提取 ID 编码
    fallback = _extract_douyin_share_id(text)
    if fallback:
        return fallback
    return None


def extract_article_url(text: str) -> str:
    """提取文章 URL（非视频）"""
    # ⭐ 2026-08-08 P4: 视频域名按 hostname 后缀匹配（子串匹配会误伤 unix.com 类域名）
    # ⭐ 2026-08-09: 移除 xiaohongshu/xhslink——小红书改由 webnote_distiller 专管
    # （图文笔记走图文管线，视频笔记由适配器内部回退视频管线）
    video_domains = ('douyin.com', 'youtube.com', 'youtu.be', 'bilibili.com', 'b23.tv',
                     'kuaishou.com',
                     'weibo.com', 'weibo.cn', 'tiktok.com', 'x.com', 'twitter.com')
    url_pattern = r'(https?://[^\s<>"{}|\^`\[\]]+)'
    match = re.search(url_pattern, text)
    if match:
        url = match.group(1).rstrip('.,;:!?)')
        try:
            from urllib.parse import urlparse
            host = (urlparse(url).hostname or '').lower()
        except Exception:
            host = ''
        if host and not any(host == d or host.endswith('.' + d) for d in video_domains):
            return url
        if not host:  # 解析失败退回旧逻辑（子串匹配），保守放行文章路由
            if not any(v in url.lower() for v in video_domains):
                return url
    return None


def is_article_url(text: str) -> bool:
    """判断是否为文章 URL"""
    return extract_article_url(text) is not None


def is_video_url(text: str) -> bool:
    """判断是否为视频 URL"""
    return extract_video_url(text) is not None


def probe_generic_video_url(url: str) -> bool:
    """通用视频探测（2026-08-08 P4 兜底，R6）：白名单未覆盖的域名链接，
    用 distiller URLResolver 探测可解析性，通过则当视频处理，失败降级文章路由。
    仅在 extract_article_url 也不匹配时由调用方触发，避免误吞文章链接。
    """
    try:
        from scripts.douyin_distiller import URLResolver
        vi = URLResolver().resolve(url)
        logger.info(f"[探测] 通用视频探测通过: {url[:60]} -> {vi.platform}")
        return bool(vi.resolved_url)
    except Exception as e:
        logger.info(f"[探测] 通用视频探测失败(降级文章): {str(e)[:80]}")
        return False


def _fetch_via_jina_fallback(url: str, timeout: int = 45) -> tuple[bool, str]:
    """Jina Reader 降级抓取（2026-08-13）：defuddle 失败后尝试。

    r.jina.ai 是第三方网页转 Markdown 服务，可绕过部分站点反爬
    （如 openai.com 对 defuddle 返回 403）。复用 zhiwei-rag 的
    web_reader 模块（已内置 18082 SOCKS 桥探活与直连回退策略）。
    返回 (success, 净化后正文)；Jina 元信息头部（Title/URL Source/
    Markdown Content 行）会被剥离。
    """
    try:
        import sys as _sys
        _rag_root = os.path.expanduser("~/zhiwei-rag")
        if _rag_root not in _sys.path:
            _sys.path.insert(0, _rag_root)
        from ingest.web_reader import get_web_markdown
        content = get_web_markdown(url)
        if not content or len(content.strip()) < 100:
            logger.warning(f"⚠️ Jina 降级抓取空或过短: {url[:60]}")
            return False, ""
        # 剥离 Jina 元信息头部
        lines = content.strip().splitlines()
        body_start = 0
        for i, ln in enumerate(lines):
            if ln.startswith("Markdown Content"):
                body_start = i + 1
                break
        body = "\n".join(lines[body_start:]).strip()
        if len(body) < 100:
            logger.warning(f"⚠️ Jina 正文过短: {url[:60]}")
            return False, ""
        # ⭐ 净化防护：登录页/拦截页常以图片为主（如飞书 applink 落地页），
        # 剥离图片与链接语法后纯文字过短即判定无效
        import re as _re_jina
        _text_only = _re_jina.sub(r'!\[[^\]]*\]\([^)]*\)', '', body)
        _text_only = _re_jina.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', _text_only)
        if len(_text_only.strip()) < 200:
            logger.warning(f"⚠️ Jina 内容以图片/链接为主或实质文字过少，判定无效: {url[:60]}")
            return False, ""
        logger.info(f"✅ Jina 降级抓取成功: {url[:50]} ({len(body)} 字符)")
        return True, body
    except Exception as e:
        logger.warning(f"⚠️ Jina 降级抓取异常: {str(e)[:100]}")
        return False, ""


def fetch_url_content(url: str, timeout: int = 30) -> tuple[bool, str]:
    """
    抓取 URL 内容，使用宿主机 defuddle；失败自动降级 Jina Reader
    返回: (success, content)
    """
    try:
        # ⭐ 2026-08-19 修复: GUI 子进程 PATH 缺 /opt/homebrew/bin，裸命令名 subprocess 调用
        # defuddle 会抛 FileNotFoundError 被误报「未安装」；改为解析绝对路径并显式补 PATH
        defuddle_bin = shutil.which("defuddle")
        if not defuddle_bin:
            _brew_defuddle = "/opt/homebrew/bin/defuddle"
            if os.path.isfile(_brew_defuddle):
                defuddle_bin = _brew_defuddle
        if not defuddle_bin:
            logger.error("❌ defuddle 未安装，请运行: /opt/homebrew/bin/npm install -g defuddle")
            return False, "❌ defuddle 未安装，请运行: /opt/homebrew/bin/npm install -g defuddle"
        env = dict(os.environ)
        env["PATH"] = "/opt/homebrew/bin:" + env.get("PATH", "")
        result = subprocess.run(
            [defuddle_bin, "parse", url, "--md"],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env
        )
        if result.returncode == 0 and result.stdout.strip():
            logger.info(f"✅ defuddle 抓取成功: {url[:50]}")
            return True, result.stdout.strip()
        else:
            # ⭐ 2026-08-13 降级链：defuddle 失败 → Jina Reader 再试一次
            if result.stderr:
                logger.warning(f"⚠️ defuddle 抓取失败: {result.stderr[:200]}")
            else:
                logger.warning(f"⚠️ defuddle 抓取失败: 无输出 ({url[:80]})")
            ok2, content2 = _fetch_via_jina_fallback(url)
            if ok2:
                return True, content2
            return False, f"❌ 网页抓取失败（已尝试 defuddle 与 Jina Reader，均失败）: {url[:80]}"
    except subprocess.TimeoutExpired:
        logger.error(f"❌ defuddle 超时: {url[:50]}")
        return False, "❌ 网页抓取超时"
    except FileNotFoundError:
        logger.error("❌ defuddle 未安装，请运行: npm install -g defuddle")
        return False, "❌ defuddle 未安装"
    except Exception as e:
        logger.error(f"❌ defuddle 抓取异常: {e}")
        return False, f"❌ 网页处理异常: {str(e)}"


def summarize_url(url: str) -> str:
    """总结网页 URL，统一调用 zhiwei-rag 的 url_ingest 引擎 (v6.0)
    
    优势：支持 v5.4 的博主式提炼、自动 RAG 关联和更鲁棒的抓取逻辑。
    """
    try:
        logger.info(f"🌐 正在调用 url_ingest 蒸馏网页: {url}")
        
        rag_venv = "/Users/liufang/zhiwei-rag/venv/bin/python3"
        # ⭐ v70.3 勘误: 真身在 scripts/ 下（原 ingest/ 路径不存在, 网页蒸馏一直静默失败）
        url_ingest_script = "/Users/liufang/zhiwei-rag/scripts/url_ingest.py"
        
        # 调用 url_ingest 并启用蒸馏模式
        # 注意：这里我们只取 stdout 返回的摘要内容
        cmd = [
            rag_venv, url_ingest_script, url, 
            "--distill", 
            "--output", "stdout" # 假设 url_ingest 支持此参数或我们通过解析日志获取
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        
        if result.returncode == 0:
            summary = result.stdout.strip()
            if "📄 **网页摘要**" in summary or len(summary) > 50:
                return summary
            return f"📄 **网页摘要**\n\n{summary}"
        else:
            logger.error(f"url_ingest 失败: {result.stderr}")
            return f"❌ 网页总结失败 (url_ingest 异常)"

    except Exception as e:
        logger.error(f"summarize_url 异常: {e}")
        return f"❌ 网页处理异常: {str(e)}"


def handle_video_async(text: str, message_id: str, user_id: str, instruction: str = None):
    """异步处理视频分析"""
    def _process():
        try:
            response = process_video(text, message_id, user_id=user_id, instruction=instruction)
            reply_message(message_id, response)
            TaskLogger.log_task("视频分析", "完成", extract_video_url(text))
        except Exception as e:
            print(f"❌ 视频分析异步处理异常: {e}")
            reply_message(message_id, f"❌ 视频分析失败: {str(e)}")

    thread = threading.Thread(target=_process, daemon=True)
    thread.start()


# ============ 文章抓取与提炼（2026-08-05 共性修复：文章 URL 不再落空） ============

_WECHAT_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
              "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.47")


def fetch_wechat_article(url: str, timeout: int = 30):
    """抓取微信公众号文章正文。返回 (success, title, text)。

    用 MicroMessenger UA 绕过微信反爬墙（实测可拿到完整正文）。
    """
    import requests as _req
    try:
        resp = _req.get(url, headers={"User-Agent": _WECHAT_UA}, timeout=timeout)
        resp.raise_for_status()
        html = resp.text
        if "环境异常" in html or "完成验证后即可" in html:
            return False, "", "微信要求人机验证，暂无法自动抓取"
        m = re.search(r'og:title"\s+content="([^"]*)"', html)
        title = m.group(1) if m else ""
        m = re.search(r'<div[^>]*id="js_content"[^>]*>(.*?)(?:</div>\s*<script|<script)',
                      html, re.DOTALL)
        if not m:
            m = re.search(r'<div[^>]*id="js_content"[^>]*>(.*)', html, re.DOTALL)
        body_html = m.group(1) if m else ""
        text = re.sub(r'<script.*?</script>', '', body_html, flags=re.DOTALL)
        text = re.sub(r'<style.*?</style>', '', text, flags=re.DOTALL)
        text = re.sub(r'<[^>]+>', '\n', text)
        for ent, ch in (('&nbsp;', ' '), ('&amp;', '&'), ('&lt;', '<'),
                        ('&gt;', '>'), ('&quot;', '"')):
            text = text.replace(ent, ch)
        text = re.sub(r'\n\s*\n+', '\n', text)
        text = '\n'.join(l.strip() for l in text.split('\n') if l.strip())
        if len(text) < 100:
            return False, title, "抓取到的正文过短，可能被反爬拦截"
        return True, title, text
    except Exception as e:
        return False, "", f"抓取异常: {e}"


def process_article(text: str, message_id: str = None, user_id: str = None) -> str:
    """抓取文章 + LLM 情报级提炼，返回回复文本。"""
    url = extract_article_url(text)
    if not url:
        return "❌ 未识别到文章链接"
    is_wechat = "mp.weixin.qq.com" in url.lower()
    if is_wechat:
        ok, title, article_text = fetch_wechat_article(url)
    else:
        ok, article_text = fetch_url_content(url)
        title = ""
    if not ok:
        return f"❌ 文章抓取失败：{article_text}"
    # ⭐ 2026-08-09: 图文页面升级——正文图片 ≥3 张时改走图文管线（VLM 读图 + 蒸馏入库），
    # 使公众号/普通网页的图文相间内容不再丢图；探测/升级失败均回退纯文本提炼
    try:
        import webnote_distiller as _wnd
        probe = (_wnd.adapt_wechat_html(None, url) if is_wechat
                 else _wnd.adapt_generic_html(url))
        if len(probe.image_urls) >= 3:
            logger.info(f"📷 检测到图文页面（{len(probe.image_urls)} 张图），升级图文管线")
            return _wnd._run_pipeline_for_note(probe, user_id)
    except Exception as e:
        logger.info(f"图文管线升级跳过（继续纯文本提炼）: {str(e)[:80]}")
    try:
        from zhiwei_common.llm import llm_client
        prompt = ("请对以下文章做情报级深度提炼，输出结构：\n"
                  "1. 一句话核心论点\n2. 关键洞察3-5条（标注事实/观点/预测）\n"
                  "3. 值得追问或存疑的点\n\n"
                  f"标题：{title}\n\n正文：\n{article_text[:12000]}")
        success, summary = llm_client.call_by_task(
            "deep_analysis", message=prompt, timeout=150)
    except Exception as e:
        success, summary = False, str(e)
    if success and summary and not summary.startswith("❌"):
        head = f"📄 《{title}》\n\n" if title else "📄 文章提炼\n\n"
        return head + summary
    # 提炼失败兜底：给正文摘要，不空手
    head = f"📄 《{title}》\n\n" if title else ""
    return head + "（抓取成功，自动提炼暂不可用，先给正文节选）\n\n" + article_text[:800]


def handle_article_async(text: str, message_id: str, user_id: str):
    """异步处理文章：抓取 + 提炼 + 回复。"""
    def _process():
        try:
            reply_message(message_id, "📖 正在读取文章并提炼，约需 30-60 秒...")
            response = process_article(text, message_id, user_id)
            reply_message(message_id, response)
            if TaskLogger:
                TaskLogger.log_task("文章提炼", "完成", extract_article_url(text))
        except Exception as e:
            print(f"❌ 文章处理异步异常: {e}")
            reply_message(message_id, f"❌ 文章处理失败: {e}")
    thread = threading.Thread(target=_process, daemon=True)
    thread.start()


def reprocess_with_instruction(user_id, artifact, instruction, message_id):
    """带用户指令重跑媒体管线（复用 distiller 转写缓存，不重新下载）

    2026-08-04 P1.3: media_followup reanalyze 分支调用。artifact 为
    ConversationStore.get_last_artifact 返回的 dict。
    """
    text = f"{artifact.get('url', '')} 重新分析"  # 含链接即可走原 extract 逻辑
    handle_video_async(text, message_id, user_id, instruction=instruction)


def _wants_vision(text: str) -> bool:
    """是否启用视觉分析（抽帧 + VLM 图表/板书/架构图提取）

    2026-08-02: 默认开启（与 youtube_update 追更 job 对齐）——视频画面
    信息本就属于分析输入；无图表画面时 VisionAnalyzer 自动跳过，不浪费
    VLM 调用。`/video novision` 前缀或「纯音频」关键词可显式关闭。
    """
    stripped = text.strip().lower()
    if stripped.startswith("/video novision") or "纯音频" in text:
        return False
    return True


def _extract_md_section(text: str, heading: str, max_chars: int = 500) -> str:
    """从 markdown 里取指定二级标题下的正文（不含子标题以后的内容）"""
    pattern = re.compile(
        r"^##\s*" + re.escape(heading) + r"\s*$(.*?)(?=^##\s|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(text)
    if not m:
        return ""
    body = m.group(1).strip()
    # 去掉空行与 Obsidian 内链语法，保留可读要点
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    body = "\n".join(lines)
    return body[:max_chars].rstrip()


def _build_video_digest(output_path: str, title: str, kind: str = "视频") -> str:
    """⭐ N2 (2026-07-31): 媒体产物完成后直接回推要点摘要

    背景：旧行为只回“文件已生成 + 请到 Obsidian 查看”，而粘链接是用户
    88% 的真实用途（入站消息统计），每次都要自己去翻笔记，闭环断在最后一步。
    现从生成的笔记中抽“核心洞察/摘要/行动建议”回推；读文件失败则降级为原行为。
    kind: 2026-08-09 新增，文案适配（"视频"/"图文"），默认视频保持旧行为。
    """
    header = f"✅ {kind}知识笔记已生成\n\n📝 **{title}**"
    try:
        content = Path(output_path).read_text(errors="ignore")
    except Exception as e:
        logger.warning(f"摘要抽取失败，降级回文件路径: {e}")
        return f"{header}\n\n📁 {output_path}"

    parts = [header]
    for label, heading, limit in (
        ("💡 核心洞察", "核心洞察", 420),
        ("📊 量化指标", "量化指标", 200),
        ("✅ 行动建议", "行动建议", 260),
    ):
        body = _extract_md_section(content, heading, limit)
        if body:
            parts.append(f"{label}\n{body}")

    # 核心洞察缺失时用“摘要”兜底，避免只回一个标题
    if len(parts) == 1:
        fallback = _extract_md_section(content, "摘要", 420)
        if fallback:
            parts.append(f"📄 摘要\n{fallback}")

    # ⭐ N3 (2026-08-02): 同步笔记为飞书文档，消息改发 feishu.cn 链接，
    # 手机/任意设备可直接点开阅读全文；同步失败则降级回本地路径。
    doc_url = None
    try:
        from feishu_note_sync import sync_note_to_feishu
        synced = sync_note_to_feishu(output_path)
        if synced:
            doc_url = synced.get("doc_url")
    except Exception as e:
        logger.warning(f"飞书文档同步异常，降级回本地路径: {e}")

    if doc_url:
        parts.append(f"📄 完整笔记（点击阅读全文）: {doc_url}")
    else:
        parts.append(f"📁 完整笔记: {output_path}")
    return "\n\n".join(parts)


def _log_distiller_failure(url: str, error_type: str, stderr_tail: str = "", stdout_tail: str = "") -> None:
    """⭐ 2026-08-13: Distiller 失败现场留痕(JSONL 追加; 写文件失败不影响主流程)"""
    try:
        import json as _json
        from datetime import datetime as _dt
        _fail_log = Path.home() / "logs" / "distiller_failures.jsonl"
        _fail_log.parent.mkdir(parents=True, exist_ok=True)
        with open(_fail_log, "a", encoding="utf-8") as _f:
            _f.write(_json.dumps({
                "ts": _dt.now().isoformat(timespec="seconds"),
                "url": url,
                "error_type": error_type,
                "stderr_tail": (stderr_tail or "")[-2000:],
                "stdout_tail": (stdout_tail or "")[-2000:],
            }, ensure_ascii=False) + "\n")
    except Exception as _log_e:
        logger.warning(f"distiller_failures.jsonl 留痕失败: {_log_e}")


def process_video(text: str, message_id: str = None, user_id: str = None, instruction: str = None) -> str:
    """处理视频分析 - 调用宿主机 Distiller

    v2.0 新增：
    - 详细错误分类和记录
    - 自动重试临时性错误
    - 严重错误飞书告警
    v3.0 新增：按需视觉分析(--vision,抽帧+VLM 图表提取)
    """
    video_history = None
    url = None
    try:
        url = extract_video_url(text)
        # ⭐ 2026-08-08 P4 兜底：白名单外链接探测通过则直接使用（与 media_commands 探测同逻辑）
        if not url:
            _m = re.search(r'https?://[^\s<>"{}|\^`\[\]]+', text or "")
            if _m and probe_generic_video_url(_m.group(0).rstrip('.,;:!?)')):
                url = _m.group(0).rstrip('.,;:!?)')
        if not url:
            return "❌ 未找到有效的视频链接"
        logger.info(f"🎬 视频链接: {url}")

        # 记录开始处理
        try:
            from video_history import get_video_history
            video_history = get_video_history()
            video_history.record_start(url)
        except Exception as e:
            logger.warning(f"VideoHistory 记录开始失败: {e}")
            video_history = None

        # 调用宿主机 Distiller
        distiller_path = os.path.expanduser("~/zhiwei-bot/scripts/douyin_distiller.py")
        # 使用共享 venv (v2.0 合并后)
        venv_python = os.path.expanduser("~/zhiwei-shared-venv/bin/python")

        cmd = [
            venv_python, distiller_path,
            "--from-text", text,
            "--output-dir", os.path.expanduser("~/Documents/ZhiweiVault/70-79_个人笔记/75_视频笔记_Video-Distill"),
        ]

        # ⭐ v3.0: 按需视觉分析(--vision 隐含 --force,重跑时复用转写缓存)
        vision_mode = _wants_vision(text)
        if vision_mode:
            cmd.append("--vision")
            logger.info("🔍 视觉分析模式: 抽帧 + VLM 图表提取")

        # 根据平台选择 cookies 策略
        if "bilibili.com" in url or "b23.tv" in url:
            # B站需要从浏览器读取 cookies（AI 字幕需登录态；网页解析已改走官方 API）
            cmd.extend(["--cookies-from-browser", "chrome"])
        elif "douyin.com" in url or "iesdouyin.com" in url:
            # 抖音使用 cookies 文件
            cmd.extend(["--cookies", os.path.expanduser("~/zhiwei-bot/secrets/douyin_cookies.txt")])
        elif "youtube.com" in url or "youtu.be" in url:
            # ⭐ 2026-08-09 反转: 本机 Chrome 会话已失效(无法自动导出有效登录态),
            # 改回用 cookies 文件(用户浏览器扩展导出+yt_cookies_import 维护, 实测
            # 通过 bot 检测); 08-02 曾因此文件被吊销改读 Chrome, 现 Chrome 亦死。
            # 网络出口由 distiller 内部自动走 hysteria2 出海出口 socks5h://127.0.0.1:18090(平台感知)。
            cmd.extend(["--cookies", os.path.expanduser("~/zhiwei-bot/secrets/youtube_cookies.txt")])
        elif any(d in url for d in ("xiaohongshu.com", "xhslink.com", "kuaishou.com",
                                    "weibo.com", "weibo.cn", "tiktok.com",
                                    "x.com", "twitter.com")):
            # ⭐ 2026-08-08 P4: 新增平台统一 Chrome 登录态（小红书/快手/微博反爬需登录态；
            # TikTok/X 由 distiller 平台感知自动走 hysteria2 出海出口 18090）
            cmd.extend(["--cookies-from-browser", "chrome"])
        # 其余平台：不带 cookies（避免无谓加载抖音 cookies 而在日志里产生 "cookie" 字样干扰错误归类）

        # ⭐ 2026-08-04 P1.3: 用户指令注入(代称映射/还原)
        if instruction:
            _uid = user_id or "anon"
            _inst_path = os.path.expanduser(f"~/zhiwei-bot/tmp/instruction_{_uid}.txt")
            try:
                os.makedirs(os.path.dirname(_inst_path), exist_ok=True)
                Path(_inst_path).write_text(instruction, encoding='utf-8')
                cmd.extend(["--instruction-file", _inst_path])
                logger.info(f"[instruction] 已注入用户指令文件: {_inst_path}")
            except Exception as e:
                logger.warning(f"指令文件写入失败,跳过指令注入: {e}")

        logger.info(f"🎬 调用 Distiller: {' '.join(cmd[:3])}...")

        # ⭐ 2026-06-02: 子进程依赖预检查（超时/异常降级，不阻塞主流程）
        # 注：原 timeout=5 在系统高负载时易把"冷启动进程 + import"拖超时，
        # 误报"环境检查失败"并拦在主命令之前。改为宽松超时 + 失败降级继续执行，
        # 真正的依赖问题由下方主命令（timeout=600）自行暴露。
        try:
            check = subprocess.run(
                [venv_python, "-c", "import dotenv; import requests; import dashscope; import yt_dlp"],
                capture_output=True, text=True, timeout=20
            )
            if check.returncode != 0:
                logger.error(f"Distiller 依赖检查失败: {check.stderr.strip()[:200]}")
                return "❌ 视频分析依赖不完整，请联系管理员修复"
        except subprocess.TimeoutExpired:
            logger.warning("Distiller 依赖预检查超时（疑似系统繁忙），降级继续执行主流程")
        except Exception as _e:
            logger.warning(f"Distiller 依赖预检查异常，降级继续执行: {_e}")

        # ⭐ 2026-08-13: 海外平台代理预探活——端口未监听时快速失败,
        # 避免 distiller 在下游卡到超时才报错(与 distiller 同源: ZHIWEI_VIDEO_PROXY)
        # hostname 后缀匹配(非全子串), 防 fox.com/max.com 等误中 x.com
        from urllib.parse import urlparse
        _overseas_host = (urlparse(url).hostname or "").lower()
        if any(_overseas_host == d or _overseas_host.endswith("." + d)
               for d in ("youtube.com", "youtu.be", "tiktok.com", "x.com", "twitter.com")):
            import socket
            _proxy_url = os.getenv("ZHIWEI_VIDEO_PROXY", "socks5h://127.0.0.1:18090")
            _pu = urlparse(_proxy_url)
            # 畸形代理 URL(无 scheme/空串 → hostname 为空): warning 后跳过探活放行, 不误杀
            if not _pu.hostname:
                logger.warning(f"ZHIWEI_VIDEO_PROXY 格式异常, 跳过预探活: {_proxy_url!r}")
            else:
                try:
                    with socket.create_connection((_pu.hostname, _pu.port or 1080), timeout=2):
                        pass
                except (OSError, ValueError) as _pe:
                    logger.error(f"出海代理端口未监听: {_pu.hostname}:{_pu.port} ({_pe})")
                    _log_distiller_failure(url, "proxy_unavailable",
                                           f"出海代理端口未监听: {_pu.hostname}:{_pu.port} ({_pe})")
                    if video_history:
                        video_history.record_failed(url, "network_error",
                                                    f"出海代理端口未监听: {_pu.hostname}:{_pu.port}")
                    return "❌ 出海代理端口未监听，请联系管理员"

        try:
            # ⭐ v3.0: vision 模式含视频下载+抽帧+逐帧 VLM,耗时更长
            # ⭐ 2026-08-08 P5: 240 分钟长视频放宽超时
            _timeout = 3600 if vision_mode else 1800
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=_timeout)
        except subprocess.TimeoutExpired as e:
            # ⭐ 2026-07-27: 超时时记录 partial output，便于定位卡在哪一步
            # 2026-08-05: 移除 15fa62b 引入的 proc/_track_file 引用——process_video
            # 用的是 subprocess.run，从未定义这两个名字，超时必抛 NameError（已实测）。
            # 「子进程脱离进程组 + PID 探活」是独立改造，需另立项（见 R3）。
            if e.stdout:
                logger.error(f"Distiller timeout - stdout (last 2000 chars):\n{e.stdout[-2000:]}")
            if e.stderr:
                logger.error(f"Distiller timeout - stderr (last 2000 chars):\n{e.stderr[-2000:]}")
            _log_distiller_failure(url, "timeout",
                                   (e.stderr or "") if isinstance(e.stderr, str) else "",
                                   (e.stdout or "") if isinstance(e.stdout, str) else "")
            raise

        if result.returncode != 0:
            # 解析错误信息
            error_type, error_message = _parse_distiller_error(result.stderr)

            # ⭐ 2026-08-13: 失败现场留痕(JSONL 追加; 写文件失败不影响主流程)
            _log_distiller_failure(url, error_type, result.stderr, result.stdout)

            # 记录失败
            if video_history:
                video_history.record_failed(url, error_type, error_message)

            # 发送告警（如果是严重错误）
            if video_history:
                from video_history import VideoErrorType
                try:
                    error_type_enum = VideoErrorType(error_type)
                    video_history.send_alert(error_type_enum, url, error_message)
                except ValueError:
                    pass  # 无效的错误类型，忽略

            # 判断是否可以重试
            if video_history and video_history.can_retry(url):
                retry_count = video_history.increment_retry(url)
                logger.info(f"将自动重试 (第 {retry_count} 次)")
                # TODO: 可以在这里添加自动重试逻辑

            # ⭐ 2026-06-02: 错误脱敏，不向用户暴露堆栈
            friendly_msg = {
                "timeout": "❌ 视频分析超时（超过 15 分钟），请检查链接是否有效",
                "network_error": "❌ 视频下载失败（网络错误），请检查链接是否有效后重试",
                "cookie_expired": "❌ 平台登录态（cookies）已过期，请联系管理员更新后重试",
                "video_not_found": "❌ 视频不存在或已被删除，请检查链接是否有效",
                "video_private": "❌ 视频为私密状态，无法访问，请检查链接或权限",
                "asr_failed": "❌ 视频语音转写失败（可能网络或识别异常），请稍后重试",
                "llm_failed": "❌ 视频内容分析（LLM）失败，请稍后重试",
                "api_error": "❌ 上游接口报错（服务暂时不可用），请稍后重试",
                "module_error": "❌ 视频分析模块异常，请联系管理员",
                "unknown": "❌ 视频处理失败（内部错误），已记录日志",
            }.get(error_type, "❌ 视频处理失败，请稍后重试")
            logger.error(f"Distiller 失败 (type={error_type}): {str(error_message)[:200]}")
            return friendly_msg

        # 解析输出
        output = result.stdout
        if "✅ Done!" in output:
            # ⭐ 2026-08-16 P1-b: 部分成功留痕——distiller rc==0 但 vision 抽帧失败
            # (如 YouTube 风控拦截视频流), stderr 含 error_type=vision_failed 标记
            _vf_reason = _extract_vision_failed(result.stderr)
            if _vf_reason:
                _log_distiller_failure(url, "vision_failed", result.stderr, result.stdout)
                logger.warning(f"Distiller 部分成功但 vision 失败: {_vf_reason[:200]}")
                if video_history:
                    try:
                        from video_history import VideoErrorType
                        video_history.send_alert(VideoErrorType.VISION_FAILED, url,
                                                 _vf_reason[:300])
                    except ValueError:
                        pass  # 枚举未同步时静默, 不阻断主流程
            # 提取输出文件路径
            match = re.search(r'Output: (.+\.md)', output)
            if match:
                output_path = match.group(1)
                # 提取标题（从文件名）
                title = Path(output_path).stem
                # 记录成功
                if video_history:
                    video_history.record_done(url, title, output_path)
                # ⭐ 2026-07-31 N2: 不再只回文件路径（旧行为要求用户自己去
                # Obsidian 翻），直接把笔记里的要点摘要回推，形成闭环。
                digest = _build_video_digest(output_path, title)
                # ⭐ 2026-08-04 P1.1: 媒体产物回写 ConversationStore，供 media_followup
                # 基于本次产物追问/重析（_build_video_digest 只调一次，避免重复同步飞书文档）
                if user_id:
                    try:
                        from core.conversation_store import conversation_store
                        conversation_store.register_artifact(
                            user_id, "video", url=url, title=title,
                            note_path=output_path, summary=digest[:500])
                        conversation_store.record_turn(
                            user_id, "system",
                            f"视频《{title}》分析完成：{digest[:300]}",
                            kind="artifact_notice")
                    except Exception as e:
                        logger.warning(f"视频产物回写 ConversationStore 失败: {e}")
                return digest
            return f"✅ 视频处理完成\n\n{output[-500:]}"

        return f"⚠️ 视频处理完成但输出格式异常\n\n{output[-500:]}"

    except subprocess.TimeoutExpired:
        # 记录失败（超时）
        error_type = "timeout"
        # ⭐ 2026-08-05: 原文案硬编码「10分钟」，与实际超时不符（普通 900s / vision 1800s），
        # 用户看到的分钟数一直是错的。改为按 vision_mode 取真实值。
        error_message = f"视频分析超时（{30 if vision_mode else 15} 分钟）"
        _log_distiller_failure(url, "timeout", stderr_tail=error_message)
        if video_history and url:
            video_history.record_failed(url, error_type, error_message)
            from video_history import VideoErrorType
            video_history.send_alert(VideoErrorType.TIMEOUT, url, error_message)
        return f"❌ {error_message}"

    except Exception as e:
        # 记录失败（未知错误）
        _log_distiller_failure(url, "exception", stderr_tail=f"{type(e).__name__}: {e}")
        if video_history and url:
            video_history.record_failed(url, "unknown", str(e))
        logger.error(f"视频处理异常: {e}")
        return f"❌ 视频处理异常: {str(e)}"


def _extract_vision_failed(stderr: str) -> str:
    """从 distiller stderr 提取 vision_failed 标记(部分成功场景: rc==0 但抽帧失败)。

    Returns:
        空串=无标记; 非空=失败原因文案。
    """
    import json as _json
    for line in (stderr or "").strip().split("\n"):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                data = _json.loads(line)
                if data.get("error_type") == "vision_failed":
                    return data.get("error_message", "") or line[:300]
            except _json.JSONDecodeError:
                continue
    return ""


def _parse_distiller_error(stderr: str) -> tuple[str, str]:
    """解析 Distiller 输出的错误信息

    Args:
        stderr: Distiller 的 stderr 输出

    Returns:
        (error_type, error_message) 元组
    """
    import json

    # 尝试解析 JSON 格式的错误信息
    for line in stderr.strip().split('\n'):
        line = line.strip()
        if line.startswith('{') and line.endswith('}'):
            try:
                data = json.loads(line)
                error_type = data.get('error_type', 'unknown')
                error_message = data.get('error_message', stderr[:500])
                return error_type, error_message
            except json.JSONDecodeError:
                continue

    # 降级：根据 stderr 内容判断错误类型
    stderr_lower = stderr.lower()

    if any(kw in stderr_lower for kw in ["cookie", "登录过期", "请先登录"]):
        return "cookie_expired", stderr[:500]
    elif any(kw in stderr_lower for kw in ["network", "connection", "timeout", "refused",
                                            "proxy connection failed", "cannot connect to proxy"]):
        return "network_error", stderr[:500]
    elif any(kw in stderr_lower for kw in ["404", "not found", "不存在"]):
        return "video_not_found", stderr[:500]
    elif any(kw in stderr_lower for kw in ["private", "私密"]):
        return "video_private", stderr[:500]
    else:
        return "unknown", stderr[:500]


# ========== 语音处理 ==========

def download_audio(message_id: str, file_key: str) -> str:
    """下载飞书语音文件"""
    try:
        from lark_oapi.api.im.v1 import GetMessageResourceRequest
        tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".opus")
        tmp_path = tmp_file.name
        tmp_file.close()

        request = GetMessageResourceRequest.builder() \
            .message_id(message_id) \
            .file_key(file_key) \
            .type("file") \
            .build()
        response = client.im.v1.message_resource.get(request)

        if response.success():
            with open(tmp_path, "wb") as f:
                f.write(response.file.read())
            print(f"✅ 语音下载成功: {tmp_path} ({os.path.getsize(tmp_path)} bytes)")
            return tmp_path
        else:
            print(f"❌ 语音下载失败: {response.code}")
            return None
    except Exception as e:
        print(f"❌ 语音下载异常: {e}")
        return None


def collapse_asr_repeats(text: str, min_cycles: int = 4):
    """折叠 ASR 解码循环(mimo/whisper 遇音频尾部静音/含糊音时常见)。

    检测「同一组句子(周期1-8)连续重复>=min_cycles 次」的循环段, 只保留一个周期。
    返回 (clean_text, was_corrupted)。
    2026-08-06 实例: 末 3 句重复 35 次污染转录→蒸馏链(C4-C8 被迫降级)。
    """
    if not text:
        return text, False
    sents = re.split(r'(?<=[。？！!?])', text)
    # 归一化比较(忽略标点/空白差异)
    norm = [re.sub(r'\W+', '', s) for s in sents]
    n = len(sents)
    for k in range(1, 9):
        i = 0
        out_idx = []
        collapsed = False
        while i < n:
            c = 1
            while (i + (c + 1) * k <= n
                   and norm[i + c*k:i + (c+1)*k] == norm[i:i+k]
                   and any(norm[i:i+k])):
                c += 1
            if c >= min_cycles:
                out_idx.extend(range(i, i + k))
                collapsed = True
                i += c * k
            else:
                out_idx.append(i)
                i += 1
        if collapsed:
            return ''.join(sents[j] for j in out_idx), True
    return text, False


def transcribe_audio(audio_path: str) -> str:
    """转录语音文件为文字

    v3.3 (2026-07-31): 原单用 DashScope, 但 DASHSCOPE_API_KEY 已 401 失效,
    飞书语音消息识别一直静默失败。改为 mimo-asr 云端首选(短语音实测 4.7s/60s),
    本地 MLX Whisper 兜底——两者都不依赖已死的 DashScope key。
    """
    from pathlib import Path as _P
    audio_obj = _P(audio_path)
    try:
        cfg = AppConfig() if AppConfig else None

        # 1. mimo-asr 云端首选(飞书语音多为短语音, mimo 快且准)
        if MimoASRTranscriber and cfg and getattr(cfg, "mimo_api_key", ""):
            try:
                tr = MimoASRTranscriber(cfg.mimo_api_key, cfg.mimo_api_base, cfg.mimo_asr_model)
                res = tr.transcribe(audio_obj)
                if res and res.full_text:
                    clean, corrupted = collapse_asr_repeats(res.full_text)
                    if corrupted:
                        logger.warning(f"⚠️ mimo-asr 解码循环: {len(res.full_text)}→{len(clean)} 字, 重复段已折叠")
                    else:
                        logger.info(f"飞书语音 mimo-asr 成功: {len(res.full_text)} 字")
                    return clean
                logger.warning("mimo-asr 空结果, 降级本地 MLX")
            except Exception as e:
                logger.warning(f"mimo-asr 失败: {e}, 降级本地 MLX")

        # 2. 本地 MLX Whisper 兜底(免费, 不依赖云端 key)
        if LocalMLXWhisperTranscriber:
            try:
                local = LocalMLXWhisperTranscriber(getattr(cfg, "local_asr_model", "small") if cfg else "small")
                if local.is_available():
                    res = local.transcribe(audio_obj)
                    if res and res.full_text:
                        clean, corrupted = collapse_asr_repeats(res.full_text)
                        if corrupted:
                            logger.warning(f"⚠️ 本地 MLX 解码循环: {len(res.full_text)}→{len(clean)} 字, 重复段已折叠")
                        else:
                            logger.info(f"飞书语音 本地 MLX 成功: {len(res.full_text)} 字")
                        return clean
            except Exception as e:
                logger.error(f"本地 MLX 也失败: {e}")

        logger.error("语音转写全部失败(mimo-asr + 本地 MLX)")
        return None

    except Exception as e:
        logger.error(f"ASR 转录异常: {e}")
        return None
    finally:
        # 清理原文件
        if audio_path and os.path.exists(audio_path):
            try:
                os.remove(audio_path)
            except: pass


def _ensure_audio_format(audio_path: Path) -> Path:
    """确保音频格式符合 Recognition API 要求（16kHz 单声道）

    复用 douyin_distiller.py 的 DashScopeASRTranscriber._ensure_audio_format 逻辑。
    返回转换后的音频路径（如无需转换则返回原路径）。
    """
    try:
        # 使用 ffprobe 检查音频格式
        probe_cmd = [
            "ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=sample_rate,channels",
            "-of", "csv=p=0", str(audio_path)
        ]
        result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=30)

        if result.returncode != 0:
            logger.warning(f"ffprobe failed, using original audio")
            return audio_path

        # 解析输出：格式为 "sample_rate,channels"
        output = result.stdout.strip()
        parts = output.split(',')

        if len(parts) >= 2:
            sample_rate = int(parts[0])
            channels = int(parts[1])

            # 检查是否需要转换（非16kHz或非单声道）
            if sample_rate != 16000 or channels != 1:
                converted_path = audio_path.with_suffix(".converted.mp3")
                logger.info(f"🎵 音频转换: {sample_rate}Hz/{channels}ch → 16000Hz/1ch")

                convert_cmd = [
                    "ffmpeg", "-y", "-i", str(audio_path),
                    "-ar", "16000", "-ac", "1", "-f", "mp3",
                    str(converted_path)
                ]
                conv_result = subprocess.run(convert_cmd, capture_output=True, timeout=120)

                if conv_result.returncode == 0 and converted_path.exists():
                    return converted_path
                else:
                    logger.warning(f"ffmpeg conversion failed")
                    return audio_path
            else:
                return audio_path
        else:
            return audio_path

    except subprocess.TimeoutExpired:
        logger.warning("ffprobe/ffmpeg timeout")
        return audio_path
    except Exception as e:
        logger.warning(f"Audio format check failed: {e}")
        return audio_path


# TTS 语音回复状态管理
tts_enabled_users = set()  # 已开启 TTS 回复的用户集合

# 全局依赖（由 init_media_handler 注入）
send_audio_reply = None  # 飞书语音发送函数


def init_media_handler_with_audio(global_send_audio_reply):
    """初始化媒体处理模块的语音发送依赖"""
    global send_audio_reply
    send_audio_reply = global_send_audio_reply


def text_to_speech_reply(text: str, message_id: str) -> bool:
    """将文字通过 TTS 转为语音并发送

    Args:
        text: 要转换的文本
        message_id: 原始消息 ID

    Returns:
        是否成功发送
    """
    if not MimoTTSClient:
        logger.warning("MimoTTSClient 不可用，跳过 TTS 回复")
        return False

    try:
        # 获取 API key
        api_key = get_api_key(["MIMO_API_KEY", "BAILIAN_API_KEY", "CODING_PLAN_API_KEY", "DASHSCOPE_API_KEY"])
        if not api_key:
            logger.warning("MIMO_API_KEY 未配置，跳过 TTS 回复")
            return False

        # 清理文本中的 markdown 格式
        clean_text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)  # 去掉 ** 加粗
        clean_text = re.sub(r'[*_`~]', '', clean_text)          # 去掉其他格式符
        clean_text = clean_text.strip()

        if not clean_text or len(clean_text) < 2:
            return False

        # 调用 TTS
        tts_client = MimoTTSClient(api_key=api_key)
        audio_path = tts_client.synthesize(clean_text)

        if not audio_path or not os.path.exists(audio_path):
            logger.warning("TTS 合成失败，跳过语音回复")
            return False

        # 发送语音消息
        if send_audio_reply:
            result = send_audio_reply(message_id, audio_path)
        else:
            logger.warning("send_audio_reply 未初始化，跳过发送")
            result = False

        # 清理临时文件
        try:
            os.remove(audio_path)
        except OSError:
            pass

        return result

    except Exception as e:
        logger.error(f"TTS 回复异常: {e}")
        return False


# ========== 语音任务收集 ==========

def handle_voice_task_async(message_id: str, file_key: str, user_id: str):
    """异步处理语音 -> 转文字 -> 与文字完全等价的自然语言路由

    2026-08-01 重构（用户决策）: 语音 = 文字的另一种输入形式。
    转写后直接进统一路由（学习/查询/捕获/分析等意图与打字一致），
    不再写入 pending_voice、不再要求「回复确认」。识别结果先回执展示，
    识别有误可直接重说（command_handler 的 pending_voice 消费逻辑
    因不再写入而自然空转，保护文件零改动）。
    """
    try:
        # 1. 下载语音
        audio_path = download_audio(message_id, file_key)
        if not audio_path:
            reply_message(message_id, "❌ 语音下载失败，请重试")
            return

        # 2. 转录
        text = transcribe_audio(audio_path)
        if not text or not text.strip():
            reply_message(message_id, "❌ 语音识别失败，请重试")
            return
        text = text.strip()

        # 3. 回执识别结果（供用户核对，误识别可立即重说）
        reply_message(message_id, f"🎤 {text}")

        # 4. 走与文字完全相同的处理路由
        from command_handler import handle_text_async
        handle_text_async(text, user_id, message_id)

        logger.info(f"🎤 语音已路由: {user_id} - {text[:50]}...")

    except Exception as e:
        logger.error(f"语音处理异常: {e}")
        reply_message(message_id, f"❌ 语音处理异常: {str(e)}")


# ---------------------------------------------------------------------------
# PDF 文档蒸馏（2026-08-02 新增）
# 飞书文件消息(PDF) → 下载 → pdf_distiller 蒸馏成 Obsidian 笔记(Inbox)
# → 复用视频摘要回推（含 feishu_note_sync 飞书文档链接，手机可读）
# ---------------------------------------------------------------------------

def download_file_resource(message_id: str, file_key: str, suffix: str = ".pdf") -> str:
    """下载飞书文件消息附件（与 download_audio 同一 API，type=file）"""
    try:
        from lark_oapi.api.im.v1 import GetMessageResourceRequest
        tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        tmp_path = tmp_file.name
        tmp_file.close()

        request = GetMessageResourceRequest.builder() \
            .message_id(message_id) \
            .file_key(file_key) \
            .type("file") \
            .build()
        response = client.im.v1.message_resource.get(request)

        if response.success():
            with open(tmp_path, "wb") as f:
                f.write(response.file.read())
            print(f"✅ 文件下载成功: {tmp_path} ({os.path.getsize(tmp_path)} bytes)")
            return tmp_path
        print(f"❌ 文件下载失败: {response.code}")
        return None
    except Exception as e:
        print(f"❌ 文件下载异常: {e}")
        return None


def process_pdf(message_id: str, file_key: str, file_name: str) -> str:
    """PDF 蒸馏主流程：下载 → 子进程蒸馏 → 复用 _build_video_digest 回推

    v70.6: 全程 task_journal 留痕，进程中断可被看门狗断点续跑。
    """
    pdf_path = download_file_resource(message_id, file_key, suffix=".pdf")
    if not pdf_path:
        return "❌ PDF 下载失败，请重试"

    journal_id = task_journal.record_start("pdf", file_name, message_id, file_key)
    try:
        size_mb = os.path.getsize(pdf_path) / 1024 / 1024
        if size_mb > 50:
            task_journal.record_failed(journal_id, f"过大 {size_mb:.0f}MB")
            return f"❌ PDF 过大（{size_mb:.0f}MB > 50MB），暂不支持"

        venv_python = os.path.expanduser("~/zhiwei-shared-venv/bin/python")
        distiller_path = os.path.expanduser("~/zhiwei-bot/scripts/pdf_distiller.py")
        cmd = [venv_python, distiller_path,
               "--pdf", pdf_path,
               "--output-dir", os.path.expanduser("~/Documents/ZhiweiVault/Inbox")]

        logger.info(f"📄 调用 PDF Distiller: {file_name} ({size_mb:.1f}MB)")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        except subprocess.TimeoutExpired:
            task_journal.record_failed(journal_id, "蒸馏超时 900s")
            return "❌ PDF 分析超时（超过 15 分钟），文件可能过大"

        if result.returncode != 0:
            logger.error(f"PDF Distiller 失败: {result.stderr[-400:]}")
            task_journal.record_failed(journal_id, (result.stderr or "rc!=0")[-200:])
            if "扫描件" in result.stderr:
                return "❌ 该 PDF 是扫描件/图片型，提取不到文字（OCR 支持后续再加）"
            return "❌ PDF 分析失败（内部错误），已记录日志"

        match = re.search(r'Output: (.+\.md)', result.stdout)
        if match:
            output_path = match.group(1)
            # 复用视频摘要回推：抽取核心洞察 + 同步飞书文档给可点链接
            digest = _build_video_digest(output_path, Path(output_path).stem)
            task_journal.record_done(journal_id)
            return digest
        task_journal.record_failed(journal_id, "distiller 输出无 Output 路径")
        return f"✅ PDF 处理完成\n\n{result.stdout[-300:]}"
    except Exception as e:
        task_journal.record_failed(journal_id, str(e)[:200])
        raise
    finally:
        try:
            os.remove(pdf_path)
        except OSError:
            pass


def handle_pdf_async(message_id: str, file_key: str, file_name: str, user_id: str):
    """异步处理 PDF 文档蒸馏"""
    def _process():
        try:
            response = process_pdf(message_id, file_key, file_name)
            reply_message(message_id, response)
            TaskLogger.log_task("PDF蒸馏", "完成", file_name)
        except Exception as e:
            print(f"❌ PDF 异步处理异常: {e}")
            reply_message(message_id, f"❌ PDF 处理失败: {str(e)}")

    thread = threading.Thread(target=_process, daemon=True)
    thread.start()


# ---------------------------------------------------------------------------
# 音频文件蒸馏（2026-08-02 新增）
# 飞书文件消息(m4a/mp3/...) → 下载 → audio_distiller 转写+蒸馏成笔记(Inbox)
# → 复用视频摘要回推（含 feishu_note_sync 飞书文档链接，手机可读）
# ---------------------------------------------------------------------------

AUDIO_FILE_EXTS = (".m4a", ".mp3", ".wav", ".aac", ".ogg", ".flac")
# ⭐ 2026-08-08 P6: 视频文件扩展名（微信视频号等无 URL 场景的本地文件入口）
VIDEO_FILE_EXTS = (".mp4", ".mov", ".mkv", ".webm", ".m4v")


def process_local_video_file(message_id: str, file_key: str, file_name: str) -> str:
    """本地视频文件蒸馏主流程（2026-08-08 P6）：下载 → distiller --local-file 全链路
    → 飞书文档同步 → artifact 注册（可被文档评论追问）→ 摘要回推
    """
    suffix = Path(file_name).suffix.lower() or ".mp4"
    video_path = download_file_resource(message_id, file_key, suffix=suffix)
    if not video_path:
        return "❌ 视频文件下载失败，请重试"

    journal_id = task_journal.record_start("video_file", file_name, message_id, file_key)
    try:
        size_mb = os.path.getsize(video_path) / 1024 / 1024
        if size_mb > 2048:
            task_journal.record_failed(journal_id, f"过大 {size_mb:.0f}MB")
            return f"❌ 视频过大（{size_mb:.0f}MB > 2GB），暂不支持"

        venv_python = os.path.expanduser("~/zhiwei-shared-venv/bin/python")
        distiller_path = os.path.expanduser("~/zhiwei-bot/scripts/douyin_distiller.py")
        cmd = [venv_python, distiller_path, "--local-file", video_path,
               "--output-dir", os.path.expanduser("~/Documents/ZhiweiVault/70-79_个人笔记/75_视频笔记_Video-Distill"),
               "--json", "--vision"]

        logger.info(f"🎬 本地视频蒸馏: {file_name} ({size_mb:.1f}MB)")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        except subprocess.TimeoutExpired:
            task_journal.record_failed(journal_id, "处理超时 3600s")
            return "❌ 视频处理超时（超过 60 分钟），文件可能过长"

        if result.returncode != 0:
            err = (result.stderr or result.stdout or "")[-300:]
            task_journal.record_failed(journal_id, err[:200])
            return f"❌ 视频蒸馏失败：{err[:200]}"

        note_path, title = "", Path(file_name).stem
        try:
            out = json.loads(result.stdout.strip().splitlines()[-1])
            note_path = out.get("output_path", "")
            title = out.get("title", title)
        except (json.JSONDecodeError, IndexError):
            pass

        # 飞书文档同步 + artifact 注册（失败降级不阻断；文档同步后即获得评论追问能力）
        feishu_url = ""
        if note_path:
            try:
                from feishu_note_sync import sync_note_to_feishu
                synced = sync_note_to_feishu(note_path)
                if synced:
                    feishu_url = synced.get("doc_url", "")
            except Exception as e:
                logger.warning(f"本地视频飞书同步降级: {e}")
            try:
                from core.conversation_store import conversation_store
                conversation_store.register_artifact(
                    "local_file", "video", url=f"file://{video_path}", title=title,
                    note_path=note_path, source="manual", feishu_url=feishu_url or None)
            except Exception as e:
                logger.warning(f"本地视频 artifact 注册降级: {e}")

        task_journal.record_done(journal_id)
        digest = _build_video_digest(note_path, title) if note_path else f"✅ 视频笔记已生成：{title}"
        if feishu_url:
            digest += f"\n\n📄 飞书文档：{feishu_url}\n（可在文档内评论追问，答案自动沉淀进笔记）"
        return digest
    except Exception as e:
        try:
            task_journal.record_failed(journal_id, str(e)[:200])
        except Exception:
            pass
        return f"❌ 视频处理异常：{e}"
    finally:
        try:
            if video_path and os.path.exists(video_path):
                os.remove(video_path)
        except Exception:
            pass


def handle_local_video_file_async(message_id: str, file_key: str, file_name: str, user_id: str):
    """异步处理本地视频文件蒸馏（兼容入口：音频通道也会先走这里判扩展名）"""
    def _process():
        try:
            response = process_local_video_file(message_id, file_key, file_name)
            reply_message(message_id, response)
            TaskLogger.log_task("视频文件蒸馏", "完成", file_name)
        except Exception as e:
            print(f"❌ 视频文件异步处理异常: {e}")
            reply_message(message_id, f"❌ 视频处理失败: {str(e)}")

    thread = threading.Thread(target=_process, daemon=True)
    thread.start()


def process_audio_file(message_id: str, file_key: str, file_name: str) -> str:
    """音频文件蒸馏主流程：下载 → 子进程转写+蒸馏 → 复用 _build_video_digest 回推

    v70.6: 全程 task_journal 留痕，进程中断可被看门狗断点续跑。
    ⭐ 2026-08-08 P6: 兼容入口——视频扩展名转本地视频链路（ws_client 受保护，
    视频文件消息需经音频通道接入，此处按扩展名分流）。
    """
    suffix = Path(file_name).suffix.lower() or ".m4a"
    if suffix in VIDEO_FILE_EXTS:
        return process_local_video_file(message_id, file_key, file_name)
    audio_path = download_file_resource(message_id, file_key, suffix=suffix)
    if not audio_path:
        return "❌ 音频下载失败，请重试"

    journal_id = task_journal.record_start("audio", file_name, message_id, file_key)
    try:
        size_mb = os.path.getsize(audio_path) / 1024 / 1024
        if size_mb > 100:
            task_journal.record_failed(journal_id, f"过大 {size_mb:.0f}MB")
            return f"❌ 音频过大（{size_mb:.0f}MB > 100MB），暂不支持"

        venv_python = os.path.expanduser("~/zhiwei-shared-venv/bin/python")
        distiller_path = os.path.expanduser("~/zhiwei-bot/scripts/audio_distiller.py")
        cmd = [venv_python, distiller_path,
               "--audio", audio_path,
               "--output-dir", os.path.expanduser("~/Documents/ZhiweiVault/Inbox")]

        logger.info(f"🎧 调用 Audio Distiller: {file_name} ({size_mb:.1f}MB)")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        except subprocess.TimeoutExpired:
            task_journal.record_failed(journal_id, "处理超时 1800s")
            return "❌ 音频处理超时（超过 30 分钟），文件可能过长"

        if result.returncode != 0:
            logger.error(f"Audio Distiller 失败: {result.stderr[-400:]}")
            task_journal.record_failed(journal_id, (result.stderr or "rc!=0")[-200:])
            if "转写" in result.stderr:
                return "❌ 音频转写失败（云端和本地识别均未成功），已记录日志"
            return "❌ 音频分析失败（内部错误），已记录日志"

        match = re.search(r'Output: (.+\.md)', result.stdout)
        if match:
            output_path = match.group(1)
            # 复用视频摘要回推：抽取核心洞察 + 同步飞书文档给可点链接
            digest = _build_video_digest(output_path, Path(output_path).stem)
            task_journal.record_done(journal_id)
            return digest
        task_journal.record_failed(journal_id, "distiller 输出无 Output 路径")
        return f"✅ 音频处理完成\n\n{result.stdout[-300:]}"
    except Exception as e:
        task_journal.record_failed(journal_id, str(e)[:200])
        raise
    finally:
        try:
            os.remove(audio_path)
        except OSError:
            pass


def handle_audio_file_async(message_id: str, file_key: str, file_name: str, user_id: str):
    """异步处理音频文件蒸馏"""
    def _process():
        try:
            response = process_audio_file(message_id, file_key, file_name)
            reply_message(message_id, response)
            TaskLogger.log_task("音频蒸馏", "完成", file_name)
        except Exception as e:
            print(f"❌ 音频文件异步处理异常: {e}")
            reply_message(message_id, f"❌ 音频处理失败: {str(e)}")

    thread = threading.Thread(target=_process, daemon=True)
    thread.start()
