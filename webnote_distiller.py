"""通用图文页面蒸馏链路（2026-08-09 新增）

适配器架构：小红书 / 知乎 / 微信公众号 / 通用网页 → 统一 NoteData 契约
→ 图片下载 → VLM 读图（qwen-vl-plus）→ LLM 蒸馏 → Vault 笔记 → 摘要回推。

路由约定：
- 小红书/知乎链接由 commands/media_commands.py 前置识别后调 handle_web_note_async
- 小红书视频类笔记（type=video）内部回退现有视频管线
- 微信公众号仍走 media_handler.process_article，但本模块提供 upgrade_article_with_images
  供正文图片较多时升级为图文管线
"""
import os
import re
import sys
import json
import logging
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, urljoin

import requests

logger = logging.getLogger(__name__)

DESKTOP_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

OUTPUT_DIR = os.path.expanduser(
    "~/Documents/ZhiweiVault/70-79_个人笔记/75_视频笔记_Video-Distill")

MAX_IMAGES = 12  # VLM 成本上限


class AdapterError(Exception):
    """适配器抓取/解析失败（消息直接面向用户）"""


class VideoFallback(Exception):
    """内容是视频（如小红书视频笔记），需回退视频管线"""


@dataclass
class NoteData:
    source: str            # xiaohongshu / zhihu / wechat / web
    url: str
    title: str = ""
    author: str = ""
    text: str = ""
    image_urls: list = field(default_factory=list)
    tags: list = field(default_factory=list)


# ============================================================================
# URL 识别
# ============================================================================

XHS_URL_RE = re.compile(
    r'https?://(?:(?:www\.)?xiaohongshu\.com/[^\s<>"\']+|xhslink\.com/[A-Za-z0-9/_-]+)')
ZH_URL_RE = re.compile(
    r'https?://(?:www\.|zhuanlan\.)?zhihu\.com/[^\s<>"\']+')


def extract_web_note_url(text: str):
    """识别小红书/知乎链接，返回 (url, source) 或 (None, None)"""
    m = XHS_URL_RE.search(text or "")
    if m:
        return m.group(0).rstrip('，。！？、；：）】》.,;:!?)'), "xiaohongshu"
    m = ZH_URL_RE.search(text or "")
    if m:
        return m.group(0).rstrip('，。！？、；：）】》.,;:!?)'), "zhihu"
    return None, None


# ============================================================================
# 适配器
# ============================================================================

def _strip_url_tail(url: str) -> str:
    return url.rstrip('，。！？、；：""''）】》.,;:!?)')


def _response_text(r) -> str:
    """requests 响应解码修正：响应头缺 charset 时 requests 默认 iso-8859-1，
    中文页面必乱码，改用 apparent_encoding 探测"""
    if not r.encoding or r.encoding.lower() in ('iso-8859-1', 'windows-1252'):
        r.encoding = r.apparent_encoding or 'utf-8'
    return r.text


def adapt_xiaohongshu(url: str) -> NoteData:
    """小红书笔记：免 cookie 直抓页面解析 __INITIAL_STATE__（2026-08-09 实测通过）。
    视频类笔记抛 VideoFallback 由调用方回退视频管线。"""
    sess = requests.Session()
    sess.headers.update({"User-Agent": DESKTOP_UA})
    # xhslink 短链追重定向
    if "xhslink.com" in url:
        try:
            r = sess.get(url, allow_redirects=True, timeout=15)
            url = r.url
        except Exception as e:
            raise AdapterError(f"小红书短链解析失败: {e}")
    try:
        r = sess.get(url, timeout=20)
        r.raise_for_status()
    except Exception as e:
        raise AdapterError(f"小红书页面抓取失败: {e}")
    m = re.search(r'window\.__INITIAL_STATE__\s*=\s*(.+?)</script>', _response_text(r), re.S)
    if not m:
        raise AdapterError("小红书页面结构变化，未找到笔记数据（可能被风控，稍后重试）")
    try:
        data = json.loads(m.group(1).replace('undefined', 'null'))
    except Exception as e:
        raise AdapterError(f"小红书笔记数据解析失败: {e}")
    note_map = (data.get('note') or {}).get('noteDetailMap') or {}
    note = None
    for v in note_map.values():
        note = v.get('note')
        if note:
            break
    if not note:
        raise AdapterError("小红书笔记数据为空（可能已删除或需要登录态）")
    if note.get('type') == 'video':
        raise VideoFallback(url)
    image_urls = []
    for img in (note.get('imageList') or []):
        u = img.get('urlDefault') or img.get('url') or ''
        if u:
            image_urls.append(u.replace('http://', 'https://'))
    return NoteData(
        source="xiaohongshu", url=url,
        title=(note.get('title') or '').strip(),
        author=((note.get('user') or {}).get('nickname') or '').strip(),
        text=(note.get('desc') or '').strip(),
        image_urls=image_urls,
        tags=[t.get('name') for t in (note.get('tagList') or []) if t.get('name')],
    )


def _chrome_cookie_session(domain_filter: str) -> requests.Session:
    """经 yt-dlp cookiejar 机制提取 Chrome 登录态，构造带 cookies 的 Session"""
    import yt_dlp
    sess = requests.Session()
    sess.headers.update({"User-Agent": DESKTOP_UA})
    try:
        with yt_dlp.YoutubeDL({'quiet': True, 'skip_download': True,
                               'cookiesfrombrowser': ('chrome',)}) as ydl:
            for c in ydl.cookiejar:
                if domain_filter in (c.domain or ''):
                    sess.cookies.set(c.name, c.value, domain=c.domain.lstrip('.'))
    except Exception as e:
        logger.warning(f"Chrome cookies 提取失败（{domain_filter}）: {e}")
    return sess


def _html_to_note_content(content_html: str, base_url: str):
    """知乎 content HTML → (纯文本, 图片 URL 列表)"""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(content_html or "", "html.parser")
    imgs = []
    for img in soup.find_all('img'):
        src = img.get('data-original') or img.get('data-actualsrc') or img.get('src') or ''
        if src.startswith('//'):
            src = 'https:' + src
        if src.startswith('http'):
            imgs.append(src)
    text = soup.get_text("\n")
    text = re.sub(r'\n\s*\n+', '\n', text).strip()
    return text, imgs


def adapt_zhihu(url: str) -> NoteData:
    """知乎专栏文章/回答/问题页：页面直抓被风控 403，走 v4 API + Chrome cookies
    （2026-08-09 实测 answer/question API 200 可达）。"""
    parsed = urlparse(url)
    path = parsed.path
    sess = _chrome_cookie_session('zhihu')
    sess.headers.update({"Referer": "https://www.zhihu.com/"})
    api_base = "https://www.zhihu.com/api/v4"

    def _get_api(api_url: str):
        try:
            r = sess.get(api_url, timeout=20)
        except Exception as e:
            raise AdapterError(f"知乎 API 请求失败: {e}")
        if r.status_code == 403:
            raise AdapterError("知乎风控拦截（403）：请在 Chrome 保持知乎登录态后重试")
        if r.status_code != 200:
            raise AdapterError(f"知乎 API 返回 HTTP {r.status_code}（链接可能无效）")
        try:
            return r.json()
        except Exception:
            raise AdapterError("知乎 API 返回非 JSON（可能被风控）")

    m = re.match(r'/p/(\d+)', path)
    if m:  # 专栏文章
        d = _get_api(f"{api_base}/articles/{m.group(1)}")
        text, imgs = _html_to_note_content(d.get('content', ''), url)
        return NoteData(source="zhihu", url=url, title=d.get('title', ''),
                        author=((d.get('author') or {}).get('name') or ''),
                        text=text, image_urls=imgs)

    m = re.match(r'/question/(\d+)/answer/(\d+)', path)
    if m:  # 回答
        qid, aid = m.group(1), m.group(2)
        d = _get_api(f"{api_base}/answers/{aid}?include=content,excerpt,question")
        text, imgs = _html_to_note_content(d.get('content', ''), url)
        # API 可能返回截断/过短内容（无截断标记但正文异常短），回退抓回答页 HTML
        if len(text) < 50 and not imgs:
            logger.info(f"[webnote] 知乎回答 API 内容过短，回退页面抓取: {aid}")
            try:
                r = sess.get(f"https://www.zhihu.com/question/{qid}/answer/{aid}", timeout=20)
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(r.text, "html.parser")
                rt = soup.find('div', class_='RichText') or soup.find('div', class_='AnswerItem')
                if rt:
                    text, imgs = _html_to_note_content(str(rt), url)
            except Exception as e:
                logger.warning(f"[webnote] 知乎回答页回退抓取失败: {str(e)[:80]}")
        qtitle = ((d.get('question') or {}).get('title') or '')
        return NoteData(source="zhihu", url=url, title=qtitle,
                        author=((d.get('author') or {}).get('name') or ''),
                        text=text, image_urls=imgs)

    m = re.match(r'/question/(\d+)', path)
    if m:  # 问题页 → 取高赞首答（问题标题用 answers 返回的内嵌 question，
        qid = m.group(1)  # /questions/{qid} 元信息 API 实测被风控 403，不再调用
        d = _get_api(f"{api_base}/questions/{qid}/answers?limit=1&sort_by=default"
                     f"&include=data%5B*%5D.content,author")
        items = d.get('data') or []
        if not items:
            raise AdapterError("知乎问题下暂无可抓取的回答")
        ans = items[0]
        text, imgs = _html_to_note_content(ans.get('content', ''), url)
        qtitle = ((ans.get('question') or {}).get('title') or '')
        return NoteData(source="zhihu", url=url, title=qtitle,
                        author=((ans.get('author') or {}).get('name') or ''),
                        text=text, image_urls=imgs)

    raise AdapterError(f"暂不支持的知乎链接类型: {path}（支持专栏文章/回答/问题页）")


def adapt_wechat_html(html: str, url: str) -> NoteData:
    """微信公众号：media_handler 已抓到 HTML 时由本适配器补抽正文图片。
    也支持直接传 URL（内部抓取）。"""
    if html is None:
        try:
            r = requests.get(url, headers={"User-Agent": DESKTOP_UA}, timeout=20)
            html = _response_text(r)
        except Exception as e:
            raise AdapterError(f"公众号页面抓取失败: {e}")
    m = re.search(r'og:title"\s+content="([^"]*)"', html)
    title = m.group(1) if m else ""
    m = re.search(r'<div[^>]*id="js_content"[^>]*>(.*?)(?:</div>\s*<script|<script)',
                  html, re.DOTALL)
    if not m:
        m = re.search(r'<div[^>]*id="js_content"[^>]*>(.*)', html, re.DOTALL)
    body_html = m.group(1) if m else ""
    imgs = [u.replace('http://', 'https://')
            for u in re.findall(r'data-src="(https?://[^"]+)"', body_html)]
    text = re.sub(r'<script.*?</script>', '', body_html, flags=re.DOTALL)
    text = re.sub(r'<style.*?</style>', '', text, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', '\n', text)
    for ent, ch in (('&nbsp;', ' '), ('&amp;', '&'), ('&lt;', '<'),
                    ('&gt;', '>'), ('&quot;', '"')):
        text = text.replace(ent, ch)
    text = re.sub(r'\n\s*\n+', '\n', text)
    text = '\n'.join(l.strip() for l in text.split('\n') if l.strip())
    return NoteData(source="wechat", url=url, title=title, text=text, image_urls=imgs)


_ICON_HINTS = ('icon', 'logo', 'avatar', 'emoji', 'qrcode', 'favicon')


def adapt_generic_html(url: str) -> NoteData:
    """通用网页兜底：requests + bs4 主文本 + 正文区图片（启发式过滤小图标）"""
    from bs4 import BeautifulSoup
    try:
        r = requests.get(url, headers={"User-Agent": DESKTOP_UA}, timeout=20)
        r.raise_for_status()
    except Exception as e:
        raise AdapterError(f"网页抓取失败: {e}")
    if not _response_text(r) or len(r.text) < 500:
        raise AdapterError("网页内容为空或抓取被拦截")
    soup = BeautifulSoup(r.text, "html.parser")
    title = (soup.title.get_text(strip=True) if soup.title else "")
    for tag in soup(['script', 'style', 'nav', 'header', 'footer', 'aside', 'form']):
        tag.decompose()
    main = (soup.find('article') or soup.find('main')
            or soup.find('div', id=re.compile(r'content|article|post', re.I))
            or soup.find('div', class_=re.compile(r'content|article|post', re.I))
            or soup.body or soup)
    imgs = []
    for img in main.find_all('img'):
        src = img.get('data-src') or img.get('src') or ''
        src = urljoin(url, src)
        if not src.startswith('http'):
            continue
        low = src.lower()
        if any(h in low for h in _ICON_HINTS):
            continue
        w = img.get('width', '')
        if w and w.isdigit() and int(w) < 100:
            continue
        imgs.append(src)
    text = main.get_text("\n")
    text = re.sub(r'\n\s*\n+', '\n', text).strip()
    return NoteData(source="web", url=url, title=title, text=text, image_urls=imgs)


def dispatch_adapter(source: str, url: str) -> NoteData:
    if source == "xiaohongshu":
        return adapt_xiaohongshu(url)
    if source == "zhihu":
        return adapt_zhihu(url)
    if source == "wechat":
        return adapt_wechat_html(None, url)
    return adapt_generic_html(url)


# ============================================================================
# VLM 读图
# ============================================================================

IMAGE_PROMPT = """请分析这张图片并提取全部关键信息，用于后续知识蒸馏：
1. 完整提取图中可见文字（OCR）：标题、正文、标注、清单、表格内容；
2. 若图中出现具体产品/物品/品牌，逐一列出名称、外观特征、标注的功效/用途/价格；
3. 图片传达的核心要点（3-6 条）；
4. 若为纯装饰/无信息量图片，直接说明"无实质信息"。
要求：输出紧凑、信息完整，不要寒暄。"""


def _get_vlm_engine():
    """复用 zhiwei-rag VLMEngine（qwen-vl-plus 云端，与视频 vision 同一引擎）"""
    rag_root = os.path.expanduser("~/zhiwei-rag")
    if rag_root not in sys.path:
        sys.path.insert(0, rag_root)
    from multimodal.vlm_engine import VLMEngine
    try:
        from zhiwei_common import get_asr_key
        api_key = get_asr_key() or ""
    except Exception:
        api_key = ""
    return VLMEngine(model_name="qwen-vl-plus", prefer_local=False, api_key=api_key)


def _download_images(image_urls: list, dest_dir: Path) -> list:
    """下载图片到本地目录，返回 [(local_path, origin_url)]；单图失败不阻断。"""
    sess = requests.Session()
    sess.headers.update({"User-Agent": DESKTOP_UA})
    results = []
    for i, u in enumerate(image_urls[:MAX_IMAGES]):
        try:
            referer = None
            host = urlparse(u).hostname or ''
            if 'xhscdn.com' in host:
                referer = 'https://www.xiaohongshu.com/'
            elif 'zhimg.com' in host:
                referer = 'https://www.zhihu.com/'
            headers = {"Referer": referer} if referer else {}
            r = sess.get(u, headers=headers, timeout=30)
            r.raise_for_status()
            ctype = r.headers.get('Content-Type', '')
            ext = '.jpg' if 'png' not in ctype else '.png'
            if 'gif' in ctype:
                ext = '.gif'
            elif 'webp' in ctype:
                ext = '.webp'
            p = dest_dir / f"img_{i + 1:02d}{ext}"
            p.write_bytes(r.content)
            results.append((p, u))
        except Exception as e:
            logger.warning(f"[webnote] 图片 {i + 1} 下载失败: {str(e)[:80]}")
    return results


def _describe_images(local_images: list) -> list:
    """VLM 逐图描述，返回 [(path, description)]；VLM 不可用时返回空列表。"""
    try:
        vlm = _get_vlm_engine()
    except Exception as e:
        logger.warning(f"[webnote] VLM 引擎初始化失败，降级纯文本蒸馏: {e}")
        return []
    out = []
    for i, (p, _) in enumerate(local_images):
        try:
            res = vlm.describe_image(str(p), prompt=IMAGE_PROMPT, max_tokens=900)
            desc = (res.description or '').strip()
            if desc:
                out.append((p, desc))
                logger.info(f"[webnote] 图 {i + 1} VLM 完成: {desc[:60]}...")
        except Exception as e:
            logger.warning(f"[webnote] 图 {i + 1} VLM 失败: {str(e)[:80]}")
    return out


# ============================================================================
# 蒸馏与笔记产出
# ============================================================================

DISTILL_PROMPT = """请基于以下图文内容做深度提炼。若内容涉及具体产品/工具/书目等实体，必须逐一列出（名称/品牌、功效或用途、价格或使用方式，缺失则注明"未提及"）；再给出核心洞察与可操作建议。输出结构：

## 核心洞察
（3-5 条，标注事实/观点）

## 产品与要点清单
（逐条列出提及的实体及其关键信息；非产品类内容则输出关键要点清单）

## 行动建议
（2-4 条可执行建议）

要求：忠实原文，不臆造未提及的信息；输出纯 Markdown，不要寒暄。

---
标题：{title}
作者：{author}
来源：{source} ({url})
标签：{tags}

正文：
{text}

图片识别内容：
{image_desc}
"""


def _sanitize_filename(name: str, limit: int = 60) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n]+', ' ', name).strip()
    name = re.sub(r'\s+', ' ', name)
    return name[:limit] if name else "未命名"


def _write_note(note: NoteData, distilled: str, local_images: list) -> str:
    """写 Markdown 笔记到 Vault 机器分区，图片拷入同名 Assets 目录，返回笔记路径"""
    date_s = datetime.now().strftime("%Y-%m-%d")
    title = note.title or "未命名图文"
    stem = f"{date_s}_{_sanitize_filename(title)}"
    out_dir = Path(OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    note_path = out_dir / f"{stem}.md"
    suffix = 1
    while note_path.exists():
        note_path = out_dir / f"{stem}_{suffix}.md"
        suffix += 1

    assets_rel = ""
    if local_images:
        assets_dir = out_dir / "Assets" / stem
        assets_dir.mkdir(parents=True, exist_ok=True)
        lines = []
        for i, (p, origin) in enumerate(local_images):
            dest = assets_dir / p.name
            dest.write_bytes(p.read_bytes())
            lines.append(f"![图 {i + 1}](Assets/{stem}/{p.name})")
        assets_rel = "\n\n".join(lines)

    tags_json = json.dumps(note.tags[:8], ensure_ascii=False) if note.tags else '[]'
    frontmatter = (
        "---\n"
        f'title: "{title}"\n'
        f'source_url: "{note.url}"\n'
        f"date: {date_s}\n"
        "type: webnote_distill\n"
        f"source_platform: {note.source}\n"
        f"author: \"{note.author}\"\n"
        f"tags: {tags_json}\n"
        "rag: false\n"
        "---\n\n"
    )
    body = f"# {title}\n\n"
    if note.author:
        body += f"> 作者：{note.author} ｜ 来源：{note.source}\n\n"
    body += distilled.strip() + "\n"
    if assets_rel:
        body += f"\n## 原图\n\n{assets_rel}\n"
    if note.text:
        excerpt = note.text[:3000]
        body += f"\n## 原文正文\n\n{excerpt}\n"
        if len(note.text) > 3000:
            body += "\n（正文过长已截断）\n"
    note_path.write_text(frontmatter + body, encoding="utf-8")
    logger.info(f"[webnote] 笔记已生成: {note_path}")
    return str(note_path)


def _register_and_digest(note_path: str, note: NoteData, user_id: str, distilled: str):
    """复用视频链路的摘要回推 + 产物注册（失败不阻断）"""
    import media_handler
    try:
        digest = media_handler._build_video_digest(note_path, note.title or "图文笔记", kind="图文")
    except TypeError:
        # 兼容未升级的旧签名
        digest = media_handler._build_video_digest(note_path, note.title or "图文笔记")
    except Exception as e:
        logger.warning(f"[webnote] 摘要构建失败，降级: {e}")
        digest = f"✅ 图文笔记已生成\n\n📝 **{note.title}**\n\n{distilled[:800]}\n\n📁 {note_path}"
    if user_id:
        try:
            from core.conversation_store import conversation_store
            conversation_store.register_artifact(
                user_id, "article", url=note.url, title=note.title,
                note_path=note_path, summary=distilled[:500])
            conversation_store.record_turn(
                user_id, "system",
                f"图文《{note.title}》蒸馏完成：{distilled[:300]}",
                kind="artifact_notice")
        except Exception as e:
            logger.warning(f"[webnote] 产物回写 ConversationStore 失败: {e}")
    return digest


def process_web_note(text: str, message_id: str = None, user_id: str = None) -> str:
    """图文蒸馏主入口。小红书视频笔记抛 VideoFallback 由调用方回退。"""
    url, source = extract_web_note_url(text)
    video_history = None
    try:
        from video_history import get_video_history
        video_history = get_video_history()
        video_history.record_start(url)
    except Exception as e:
        logger.warning(f"[webnote] video_history 记录失败: {e}")
        video_history = None

    try:
        note = dispatch_adapter(source, url)
        if not note.text and not note.image_urls:
            raise AdapterError("抓取成功但未提取到正文与图片（页面可能需登录或被风控）")

        # 1. 图片下载
        local_images = []
        if note.image_urls:
            with tempfile.TemporaryDirectory(prefix="webnote_img_") as tmp:
                local_images = _download_images(note.image_urls, Path(tmp))
                # 2. VLM 读图
                described = _describe_images(local_images) if local_images else []
                image_desc = "\n\n".join(
                    f"【图 {i + 1}】{d}" for i, (_, d) in enumerate(described)
                ) or "（无可用图片识别结果）"
                # 3. LLM 蒸馏
                distilled = _distill(note, image_desc)
                # 4. 写笔记（临时目录退出前完成拷贝）
                note_path = _write_note(note, distilled, local_images)
        else:
            distilled = _distill(note, "（无图片）")
            note_path = _write_note(note, distilled, [])

        if video_history:
            video_history.record_done(url, note.title or "图文笔记", note_path)
        return _register_and_digest(note_path, note, user_id, distilled)

    except (AdapterError, VideoFallback):
        raise
    except Exception as e:
        logger.error(f"[webnote] 处理异常: {e}", exc_info=True)
        if video_history:
            video_history.record_failed(url, "unknown", str(e))
        raise AdapterError(f"图文处理内部异常: {str(e)[:120]}")


def _distill(note: NoteData, image_desc: str) -> str:
    from zhiwei_common.llm import llm_client
    prompt = DISTILL_PROMPT.format(
        title=note.title or "（无标题）",
        author=note.author or "（未知）",
        source=note.source, url=note.url,
        tags="、".join(note.tags) if note.tags else "（无）",
        text=(note.text or "（无正文）")[:8000],
        image_desc=image_desc[:6000],
    )
    success, out = llm_client.call_by_task("deep_analysis", message=prompt, timeout=180)
    if success and out and not out.startswith("❌"):
        return out
    # 蒸馏失败兜底：正文节选 + 图片描述，不空手
    fallback = f"## 核心洞察\n（LLM 蒸馏暂不可用，以下为原始内容节选）\n\n{note.text[:1500]}\n"
    if image_desc and image_desc != "（无可用图片识别结果）":
        fallback += f"\n## 图片识别\n\n{image_desc[:2000]}\n"
    return fallback


# ============================================================================
# 文章管线升级入口（供 media_handler.process_article 调用）
# ============================================================================

def upgrade_article_with_images(url: str, message_id: str = None,
                                user_id: str = None) -> str:
    """普通网页文章升级为图文管线（GenericHtmlAdapter）。"""
    note = adapt_generic_html(url)
    if not note.text and not note.image_urls:
        raise AdapterError("未提取到正文与图片")
    return _run_pipeline_for_note(note, user_id)


def upgrade_wechat_with_images(html: str, url: str, user_id: str = None) -> str:
    """微信公众号文章升级为图文管线（HTML 由调用方已抓取）。"""
    note = adapt_wechat_html(html, url)
    if not note.text and not note.image_urls:
        raise AdapterError("未提取到正文与图片")
    return _run_pipeline_for_note(note, user_id)


def _run_pipeline_for_note(note: NoteData, user_id: str) -> str:
    """process_web_note 的内部管线（跳过 URL 识别，供升级入口复用）"""
    local_images = []
    if note.image_urls:
        with tempfile.TemporaryDirectory(prefix="webnote_img_") as tmp:
            local_images = _download_images(note.image_urls, Path(tmp))
            described = _describe_images(local_images) if local_images else []
            image_desc = "\n\n".join(
                f"【图 {i + 1}】{d}" for i, (_, d) in enumerate(described)
            ) or "（无可用图片识别结果）"
            distilled = _distill(note, image_desc)
            note_path = _write_note(note, distilled, local_images)
    else:
        distilled = _distill(note, "（无图片）")
        note_path = _write_note(note, distilled, [])
    return _register_and_digest(note_path, note, user_id, distilled)


# ============================================================================
# 异步入口（仿 handle_article_async threading 模式）
# ============================================================================

def handle_web_note_async(text: str, message_id: str, user_id: str):
    """异步处理图文链接：回执 + 后台线程 + 完成回推。
    小红书视频笔记自动回退视频管线。"""
    import media_handler

    def _process():
        try:
            response = process_web_note(text, message_id, user_id)
            media_handler.reply_message(message_id, response)
            if media_handler.TaskLogger:
                url, _ = extract_web_note_url(text)
                media_handler.TaskLogger.log_task("图文蒸馏", "完成", url)
        except VideoFallback as fb:
            logger.info(f"[webnote] 视频类笔记，回退视频管线: {str(fb)[:80]}")
            media_handler.reply_message(
                message_id, "🎬 检测到视频类笔记，转视频分析（预计3-5分钟）...")
            media_handler.handle_video_async(text, message_id, user_id)
        except AdapterError as e:
            media_handler.reply_message(message_id, f"❌ {e}")
        except Exception as e:
            logger.error(f"[webnote] 异步异常: {e}", exc_info=True)
            media_handler.reply_message(message_id, f"❌ 图文处理失败: {str(e)[:120]}")

    threading.Thread(target=_process, daemon=True, name="webnote_distill").start()
