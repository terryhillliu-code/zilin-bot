# -*- coding: utf-8 -*-
"""飞书文档同步：把本地 Obsidian 笔记同步为飞书云文档

背景（2026-08-02）：机器人生成视频笔记后，飞书消息里只回本地文件路径，
手机/其他设备根本打不开。本模块在笔记生成后通过 lark-cli 把 Markdown
同步成飞书文档，消息里改发 feishu.cn 链接，任何设备都能直接阅读。

设计要点：
- 身份固定 --as bot（用户 token 已清空，device 授权需人工操作，不适合自动化）；
  文档归属应用，创建后立即把用户本人加为 full_access 协作者，
  用户在手机/任意设备的飞书里都能直接打开（出现在「共享给我」）；
- 首次运行自动创建「知微-视频笔记」文件夹并缓存 token；
- 维护 笔记路径 → doc_id 映射，重蒸馏（如 --vision 重跑）时 overwrite
  同一篇文档而非新建，避免重复文档堆积（注意：overwrite 会清空文档内
  已有的评论/标注）；应用未开通 docx 读权限时 overwrite 会失败，
  自动降级为「删旧建新」，链接会变但不会产生孤儿文档；
- 所有异常吞掉并返回 None，调用方降级为本地路径，绝不影响视频处理主流程。
"""

import json
import logging
import os
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

MAP_FILE = os.path.expanduser("~/zhiwei-bot/data/feishu_note_docs.json")
FOLDER_NAME = "知微-视频笔记"
LARK_TIMEOUT = 120  # docs +create/+update 大文档可能较慢
LARK_IDENTITY = "bot"  # user token 已清空；bot 建文档 + 授权给用户更稳
GRANT_OPEN_ID = "ou_6daf1267be205885ca7c97be80eb1a32"  # 用户本人，建文档后授予 full_access


# ---------------------------------------------------------------------------
# lark-cli 调用
# ---------------------------------------------------------------------------

def _run_lark(args: list, cwd: str = None) -> dict:
    """执行 lark-cli 并宽松解析输出。

    返回 {"ok": bool, "doc_id": str|None, "doc_url": str|None, "raw": str}
    成功判定：输出 JSON 中能提取到 doc_id / doc_url（兼容不同包装层级）。
    cwd: media-insert 要求 --file 为当前目录内的相对路径时，切换到图片目录执行。
    """
    cmd = ["lark-cli", *args, "--as", LARK_IDENTITY]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=LARK_TIMEOUT, cwd=cwd)
    except subprocess.TimeoutExpired:
        logger.error(f"lark-cli 超时({LARK_TIMEOUT}s): {' '.join(args[:2])}")
        return {"ok": False, "doc_id": None, "doc_url": None, "raw": "timeout"}
    except Exception as e:
        logger.error(f"lark-cli 调用异常: {e}")
        return {"ok": False, "doc_id": None, "doc_url": None, "raw": str(e)}

    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    if result.returncode != 0:
        logger.error(f"lark-cli 失败 rc={result.returncode}: {err[:300] or out[:300]}")
        return {"ok": False, "doc_id": None, "doc_url": None, "raw": err or out}

    doc_id = doc_url = None
    code0 = False  # 原生 API（如授权/建文件夹）成功时返回 {"code": 0, ...}
    try:
        data = json.loads(out)
        if isinstance(data, dict):
            code0 = data.get("code") == 0 or data.get("ok") is True
        # 宽松地在整个 JSON 树里找 doc_id / doc_url / token
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                for k, v in node.items():
                    if k == "doc_id" and isinstance(v, str):
                        doc_id = doc_id or v
                    elif k == "doc_url" and isinstance(v, str):
                        doc_url = doc_url or v
                    elif k == "token" and isinstance(v, str) and not doc_id:
                        doc_id = v  # create_folder 返回 data.token
                    elif isinstance(v, (dict, list)):
                        stack.append(v)
            elif isinstance(node, list):
                stack.extend(node)
    except (json.JSONDecodeError, TypeError):
        pass

    if not doc_url:
        m = re.search(r'"doc_url"\s*:\s*"([^"]+)"', out)
        if m:
            doc_url = m.group(1)
    if not doc_id:
        m = re.search(r'"doc_id"\s*:\s*"([^"]+)"', out)
        if m:
            doc_id = m.group(1)

    return {"ok": bool(doc_id or doc_url), "code0": code0,
            "doc_id": doc_id, "doc_url": doc_url, "raw": out[:500]}


def _delete_doc(doc_id: str):
    """删除文档移入回收站（overwrite 不可用时清理重跑产生的旧文档）。"""
    if not doc_id:
        return
    res = _run_lark(["api", "DELETE", f"/open-apis/drive/v1/files/{doc_id}",
                     "--params", json.dumps({"type": "docx"})])
    if not res.get("code0"):
        logger.warning(f"旧文档删除失败（不影响新建）: {res.get('raw', '')[:200]}")


def _grant_full_access(doc_id: str) -> bool:
    """把新建文档授权给用户本人（full_access），否则用户在飞书里打不开。"""
    res = _run_lark([
        "api", "POST", f"/open-apis/drive/v1/permissions/{doc_id}/members",
        "--params", json.dumps({"type": "docx", "need_notification": "false"}),
        "--data", json.dumps({"member_type": "openid", "member_id": GRANT_OPEN_ID,
                              "perm": "full_access"}),
    ])
    if res.get("code0"):
        return True
    logger.warning(f"文档授权失败（文档已建但用户可能打不开）: {res.get('raw', '')[:200]}")
    return False


# ---------------------------------------------------------------------------
# Markdown 预处理：Obsidian 笔记 → Lark-flavored Markdown
# ---------------------------------------------------------------------------

def _parse_frontmatter(text: str):
    """解析 YAML frontmatter（只做行级简单解析，不依赖 PyYAML）。

    返回 (meta: dict, body: str)；无 frontmatter 时 meta 为空、body 为原文。
    """
    if not text.startswith("---"):
        return {}, text
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.DOTALL)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        kv = re.match(r"^(\w+)\s*:\s*(.*)$", line.strip())
        if not kv:
            continue
        key, val = kv.group(1), kv.group(2).strip().strip('"').strip("'")
        if val.startswith("[") and val.endswith("]"):  # 内联列表，如 tags: ["a","b"]
            items = [x.strip().strip('"').strip("'") for x in val[1:-1].split(",")]
            meta[key] = [x for x in items if x]
        else:
            meta[key] = val
    return meta, m.group(2)


def _split_vision_section(body: str, note_dir: Path) -> tuple:
    """切出「关键画面与图表」区，返回 (主体正文, 帧列表)。

    帧列表项: {"ts": "02:12", "title": "...", "img_path": 绝对路径, "desc": "..."}
    笔记区格式: ### [mm:ss] 标题 + ![..](Assets/...) + 描述行
    （2026-08-02 新增: 帧截图随描述同步到飞书文档, 图文交错）
    """
    m = re.search(r"^## 关键画面与图表\s*\n(.*?)(?=^## |\Z)", body, re.M | re.S)
    if not m:
        return body, []
    section = m.group(1)
    main = body[:m.start()] + body[m.end():]
    frames = []
    for fm in re.finditer(
            r"### \[([^\]]+)\]\s*(.+?)\n+!\[[^\]]*\]\(([^)]+)\)\n+(.*?)(?=^### \[|\Z)",
            section, re.M | re.S):
        ts, title, rel, desc = fm.group(1), fm.group(2).strip(), fm.group(3).strip(), fm.group(4).strip()
        desc = "\n".join(ln for ln in desc.splitlines() if ln.strip())[:600]
        img = (note_dir / rel).resolve() if not rel.startswith(("http://", "https://", "/")) else Path(rel)
        frames.append({"ts": ts, "title": title, "img_path": str(img), "desc": desc})
    return main, frames


def _append_vision_frames(doc_id: str, frames: list) -> int:
    """图文交错追加「关键画面与图表」区到飞书文档。

    media-insert 只能追加到文档末尾，故按「+update append 描述 →
    +media-insert 图片」交替，保证每张图紧跟其描述。单帧失败跳过。
    返回成功插入的图片数。
    """
    if not frames:
        return 0
    _run_lark(["docs", "+update", "--doc", doc_id, "--mode", "append",
               "--markdown", "\n## 关键画面与图表\n"])
    ok = 0
    for f in frames:
        try:
            head = f"### [{f['ts']}] {f['title']}" if f.get("ts") else f"### {f['title']}"
            _run_lark(["docs", "+update", "--doc", doc_id, "--mode", "append",
                       "--markdown", f"\n{head}\n\n{f['desc']}\n"])
            img = f.get("img_path") or ""
            if img and Path(img).exists():
                # media-insert 仅接受当前目录内相对路径: 切到图片目录, 传文件名
                r = _run_lark(["docs", "+media-insert", "--doc", doc_id,
                               "--file", Path(img).name,
                               "--caption", str(f["title"])[:50],
                               "--align", "center"],
                              cwd=str(Path(img).parent))
                if r["ok"] or r.get("code0"):
                    ok += 1
                else:
                    logger.warning(f"图片插入失败: {Path(img).name} {r.get('raw','')[:120]}")
        except Exception as e:
            logger.warning(f"帧追加失败(跳过): {str(f.get('title',''))[:20]}: {e}")
    logger.info(f"关键画面同步: {ok}/{len(frames)} 张图片已插入")
    return ok


def _split_images_section(body: str, note_dir: Path) -> tuple:
    """切出「原图」区（图文笔记，2026-08-09 新增），返回 (主体正文, 图片列表)。

    图片列表项: {"alt": "图 1", "img_path": 绝对路径, "remote": bool}
    背景：图文笔记的原图是本地相对链接，正文预处理会被当死链剔除，
    需单独切出走 media-insert 同步（与视频关键画面同机制）。
    """
    m = re.search(r"^## 原图\s*\n(.*?)(?=^## |\Z)", body, re.M | re.S)
    if not m:
        return body, []
    section = m.group(1)
    main = body[:m.start()] + body[m.end():]
    images = []
    for im in re.finditer(r"!\[([^\]]*)\]\(([^)]+)\)", section):
        alt, rel = im.group(1).strip() or "原图", im.group(2).strip()
        remote = rel.startswith(("http://", "https://"))
        path = rel if remote else str((note_dir / rel).resolve())
        images.append({"alt": alt, "img_path": path, "remote": remote})
    return main, images


def _append_original_images(doc_id: str, images: list) -> int:
    """追加「原图」区到飞书文档：标题 + 逐张 media-insert。单图失败跳过。"""
    if not images:
        return 0
    _run_lark(["docs", "+update", "--doc", doc_id, "--mode", "append",
               "--markdown", "\n## 原图\n"])
    ok = 0
    for it in images:
        try:
            img = it.get("img_path") or ""
            if it.get("remote"):
                continue  # 外链图片 media-insert 不支持，跳过（本地笔记仍保留）
            if img and Path(img).exists():
                r = _run_lark(["docs", "+media-insert", "--doc", doc_id,
                               "--file", Path(img).name,
                               "--caption", str(it["alt"])[:50],
                               "--align", "center"],
                              cwd=str(Path(img).parent))
                if r["ok"] or r.get("code0"):
                    ok += 1
                else:
                    logger.warning(f"原图插入失败: {Path(img).name} {r.get('raw','')[:120]}")
        except Exception as e:
            logger.warning(f"原图追加失败(跳过): {str(it.get('alt',''))[:20]}: {e}")
    logger.info(f"原图同步: {ok}/{len(images)} 张图片已插入")
    return ok


def _preprocess(meta: dict, body: str, note_dir: Path = None) -> tuple:
    """把 Obsidian 笔记正文转成 Lark-flavored Markdown。

    处理：去掉首行 H1（文档标题已单独设置）、去掉 <details> 折叠块
    （飞书不支持且 ASR 原文太长）、[[wikilink]] 转纯文本、本地相对链接
    转纯文本（在飞书里是死链）。
    2026-08-02: 切出「关键画面与图表」区单独返回（帧截图走 media-insert
    同步到文档）；2026-08-09: 同机制切出图文笔记「原图」区。
    返回 (content, frames, images)。
    """
    body, frames = _split_vision_section(body, note_dir or Path.home())
    body, images = _split_images_section(body, note_dir or Path.home())
    # 去掉开头的一级标题（docs +create 的 --title 已是文档标题，禁止重复）
    body = re.sub(r"^\s*# [^\n]+\n", "", body, count=1)

    # <details><summary>ASR 转录原文</summary>...</details> → 提示语
    body = re.sub(
        r"<details>.*?</details>",
        "\n> 📼 ASR 转录原文已省略，完整原文见本地 Obsidian 笔记\n",
        body, flags=re.DOTALL,
    )

    # [[链接|显示]] → 显示；[[链接]] → 链接
    body = re.sub(r"\[\[[^\]|]*\|([^\]]+)\]\]", r"\1", body)
    body = re.sub(r"\[\[([^\]]+)\]\]", r"\1", body)

    # 本地相对链接（Assets/... 等非 http）在飞书里打不开，仅保留文字
    body = re.sub(r"\[([^\]]*)\]\((?!https?://)[^)]+\)", r"\1", body)

    # 元信息 callout 头：来源/日期/评级/标签，让文档自包含，方便后续研究溯源
    meta_lines = []
    src = meta.get("source_url", "")
    date = meta.get("date", "")
    tier = meta.get("tier", "")
    if src:
        meta_lines.append(f"**来源**：[原始视频]({src})")
    info = " ｜ ".join(x for x in (f"**日期**：{date}" if date else "",
                                  f"**评级**：{tier} 级" if tier else "") if x)
    if info:
        meta_lines.append(info)
    tags = meta.get("tags")
    if isinstance(tags, list) and tags:
        meta_lines.append("**标签**：" + " / ".join(tags))
    header = ""
    if meta_lines:
        header = ('<callout emoji="🎬" background-color="light-blue">\n'
                  + "\n".join(meta_lines)
                  + "\n</callout>\n\n")

    return header + body.strip() + "\n", frames, images


# ---------------------------------------------------------------------------
# 映射持久化 & 文件夹
# ---------------------------------------------------------------------------

def _load_map() -> dict:
    try:
        return json.loads(Path(MAP_FILE).read_text())
    except Exception:
        return {}


def _save_map(data: dict):
    try:
        Path(MAP_FILE).parent.mkdir(parents=True, exist_ok=True)
        Path(MAP_FILE).write_text(json.dumps(data, ensure_ascii=False, indent=2))
    except Exception as e:
        logger.warning(f"映射文件写入失败: {e}")


def _ensure_folder(store: dict):
    """确保云空间存在「知微-视频笔记」文件夹，返回 folder_token 或 None。"""
    token = store.get("_config", {}).get("folder_token")
    if token:
        return token
    res = _run_lark(["api", "POST", "/open-apis/drive/v1/files/create_folder",
                     "--data", json.dumps({"name": FOLDER_NAME, "folder_token": ""})])
    if res.get("doc_id"):  # create_folder 的 token 被宽松解析进 doc_id 位
        store.setdefault("_config", {})["folder_token"] = res["doc_id"]
        _save_map(store)
        logger.info(f"📁 已创建飞书文件夹「{FOLDER_NAME}」: {res['doc_id']}")
        return res["doc_id"]
    logger.warning(f"文件夹创建失败，文档将落在云空间根目录: {res.get('raw', '')[:200]}")
    return None


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def sync_note_to_feishu(md_path: str):
    """把本地 Markdown 笔记同步为飞书文档。

    已同步过的路径走 overwrite 更新同一篇文档；否则在「知微-视频笔记」
    文件夹下新建。成功返回 {"doc_id", "doc_url"}，失败返回 None。
    """
    try:
        text = Path(md_path).read_text(errors="ignore")
    except Exception as e:
        logger.error(f"笔记读取失败 {md_path}: {e}")
        return None

    meta, body = _parse_frontmatter(text)
    title = meta.get("title") or Path(md_path).stem
    content, frames, images = _preprocess(meta, body, Path(md_path).parent)

    store = _load_map()
    notes = store.setdefault("notes", {})
    entry = notes.get(md_path) or {}

    # 已同步过 → 覆盖更新原文档（重蒸馏场景，避免重复文档）
    if entry.get("doc_id"):
        res = _run_lark(["docs", "+update", "--doc", entry["doc_id"],
                         "--mode", "overwrite", "--markdown", content,
                         "--new-title", title])
        if res["ok"]:
            # 2026-08-02: 帧截图图文交错追加（图片同步失败不影响文档主体）
            try:
                _append_vision_frames(entry["doc_id"], frames)
                _append_original_images(entry["doc_id"], images)
            except Exception as e:
                logger.warning(f"图片追加异常(不影响文档): {e}")
            entry.update({"title": title})
            notes[md_path] = entry
            _save_map(store)
            logger.info(f"✅ 飞书文档已更新: {entry.get('doc_url')}")
            return {"doc_id": entry["doc_id"], "doc_url": entry.get("doc_url")}
        logger.warning("覆盖更新失败（应用缺 docx 读权限或文档已删除），删旧建新")
        _delete_doc(entry.get("doc_id"))

    # 新建文档
    folder_token = _ensure_folder(store)
    args = ["docs", "+create", "--title", title, "--markdown", content]
    if folder_token:
        args += ["--folder-token", folder_token]
    res = _run_lark(args)
    if not res["ok"]:
        logger.error(f"飞书文档创建失败: {res.get('raw', '')[:300]}")
        return None

    _grant_full_access(res["doc_id"])  # 授权失败不阻断，链接仍可尝试打开
    # 2026-08-02: 帧截图图文交错追加（图片同步失败不影响文档主体）
    try:
        _append_vision_frames(res["doc_id"], frames)
        _append_original_images(res["doc_id"], images)
    except Exception as e:
        logger.warning(f"图片追加异常(不影响文档): {e}")
    notes[md_path] = {"doc_id": res["doc_id"], "doc_url": res["doc_url"], "title": title}
    _save_map(store)
    logger.info(f"✅ 飞书文档已创建: {res['doc_url']}")
    return {"doc_id": res["doc_id"], "doc_url": res["doc_url"]}


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if len(sys.argv) < 2:
        print("用法: python3 feishu_note_sync.py <笔记.md>")
        sys.exit(1)
    result = sync_note_to_feishu(sys.argv[1])
    print(json.dumps(result, ensure_ascii=False) if result else "❌ 同步失败")
