"""概念学习命令 /learn —— 生成 Obsidian 互链概念卡片，让个人知识图谱随学习生长

流水线:
  /learn Muon
    1. LLM (zhiwei_common.llm) 生成概念档案 JSON (定义/家族树/对比/演进/关联)
    2. 本地锚定: RAG :8765 检索 + papers.db 只读查询 (命中为空不阻塞)
    3. 渲染卡片 → ~/KarpathyVault/Concepts/<概念>.md (幂等更新, 保留 created)
    4. 图谱生长: 上位/同族/关联概念建 stub 占位卡 + 家族成员挂载 + concepts.json 索引
    5. 飞书回执 (ctx.reply_message)
"""
import json
import re
import sqlite3
import time
from pathlib import Path

from zhiwei_common.llm import llm_client

VAULT_CONCEPTS = Path.home() / "KarpathyVault" / "Concepts"
INDEX_PATH = VAULT_CONCEPTS / "_index" / "concepts.json"
PAPERS_DB = "/Users/liufang/arxiv-paper-analyzer/backend/data/papers.db"

# RAG 实时入库（消除"学习了却检索不到"的搜索断层）
# 红线: 入库必须用 zhiwei-rag/venv（lancedb 只装在该 venv）
RAG_INGEST_PY = "/Users/liufang/zhiwei-rag/venv/bin/python3"
RAG_INGEST_SCRIPT = "/Users/liufang/zhiwei-rag/scripts/ingest_markdown_file.py"
RAG_REFRESH_URL = "http://localhost:8765/admin/refresh"

# stub 创建上限（防一次学习刷出过多空卡）
MAX_STUB_UPSTREAM = 2
MAX_STUB_PEERS = 4
MAX_STUB_RELATED = 5
LLM_TIMEOUT = 120

PROFILE_PROMPT = """你是 AI/ML 领域的概念学习助手，正在帮用户构建个人知识图谱。
请为概念「{concept}」生成结构化学习档案。

硬性要求:
1. 只输出 JSON 本身: 不要用 markdown 代码块包裹, 不要输出任何解释文字
2. 术语用业界标准名称(英文专有名词用英文, 如 AdamW; 中文通用概念用中文, 如 优化器)
3. comparison 每行 values 的 key 必须包含本概念和至少 2 个同族概念
4. 若概念不存在/过于冷门/你不确定, confidence 填 "low" 并尽力填写

JSON 结构(所有字段必填):
{{
  "concept": "标准概念名",
  "aliases": ["常见别名"],
  "one_liner": "一句话定义(30字以内)",
  "definition": "准确定义(100-200字): 是什么/解决什么问题/核心思想",
  "upstream": ["直接上位概念, 1-2个"],
  "domain_chain": ["领域链, 从大到小, 含上位概念"],
  "family_tree": {{"chain": ["演进链关键节点, 按发展顺序"], "note": "演进逻辑一句话"}},
  "peers": [{{"name": "同族概念", "diff": "与本概念的最大区别一句话"}}],
  "comparison": [{{"aspect": "对比维度", "values": {{"本概念": "...", "同族A": "...", "同族B": "..."}}}}],
  "timeline": [{{"year": "年份", "event": "关键事件"}}],
  "key_refs": [{{"title": "论文/博客/仓库标题", "type": "paper|blog|repo", "note": "一句话说明"}}],
  "related_concepts": ["3-6个关联概念, 将作为知识图谱双链"],
  "confidence": "high|medium|low"
}}"""


# ============ 命令入口 ============

def handle_learn_commands(text_lower, text_stripped, user_id, message_id, ctx):
    """处理 /learn, /学习 概念学习命令"""
    if not (text_lower.startswith("/learn ") or text_lower.startswith("/学习 ")):
        return False
    concept = text_stripped.split(" ", 1)[1].strip() if " " in text_stripped else ""
    if not concept:
        ctx.reply_message(message_id,
                          "❌ 请提供要学习的概念\n\n用法: /learn Muon")
        return True
    do_learn(concept, user_id, message_id, ctx)
    return True


def do_learn(concept: str, user_id: str, message_id: str, ctx) -> dict:
    """主流水线: 档案生成 → 本地锚定 → 写卡 → 图谱生长 → 回执"""
    t0 = time.time()
    ctx.reply_message(message_id, f"🌱 开始学习「{concept}」，生成概念档案中...")

    # 1. LLM 概念档案
    profile = _gen_concept_profile(concept)
    if profile is None:
        ctx.reply_message(message_id,
                          f"❌ 「{concept}」档案生成失败（LLM 调用或 JSON 解析异常），请稍后重试")
        return {}
    # 以 LLM 规范化后的名字为准（如 "muon" → "Muon"）
    name = str(profile.get("concept") or concept).strip() or concept

    # 2. 本地锚定（失败不阻塞）
    rag_hits, papers = _anchor_local(name, profile.get("aliases"))

    # 3. 渲染 + 幂等写卡
    card = _render_card(profile, rag_hits, papers)
    is_upgrade, card_path = _write_card(name, card)

    # 4. 图谱生长: stub + 家族挂载 + 关系索引
    new_stubs = _grow_graph(profile)

    # 5. 回执
    chain = " > ".join(profile.get("domain_chain") or profile.get("upstream") or [])
    evolution = " → ".join((profile.get("family_tree") or {}).get("chain") or [])
    conf = profile.get("confidence", "medium")
    conf_mark = {"high": "🟢", "medium": "🟡", "low": "🔴 内容待核实"}.get(conf, "🟡")
    lines = [
        f"✅ 已学会「{name}」" + ("（stub 已升级为正式卡片）" if is_upgrade else ""),
        "",
        f"📌 {profile.get('one_liner', '')}",
    ]
    if chain:
        lines.append(f"🌳 家族: {chain}")
    if evolution:
        lines.append(f"🧬 演进: {evolution}")
    if conf == "low":
        grow_line = "⏸ 图谱未生长（内容待核实，人工确认后可 /learn 重学）"
    else:
        grow_line = (f"🌱 图谱生长: +{len(new_stubs)} stub"
                     + (f"（{' / '.join(new_stubs[:6])}）" if new_stubs else ""))
    lines += [
        f"📄 卡片: Concepts/{_safe_filename(name)}.md",
        grow_line,
        f"📚 本地锚定: RAG 命中 {len(rag_hits)} 条 · 库内论文 {len(papers)} 篇",
        f"{conf_mark} confidence: {conf}",
        "",
        f"⏱ 耗时 {time.time() - t0:.0f}s · 用 Obsidian 打开 KarpathyVault 查看知识图谱",
    ]
    ctx.reply_message(message_id, "\n".join(lines))

    # 6. RAG 实时入库 + 索引刷新（后台执行，不阻塞回执；stub 不入库）
    _trigger_ingest(card_path)
    return profile


def _trigger_ingest(card_path: Path):
    """后台调 zhiwei-rag/venv 入库卡片并刷新检索索引

    失败静默（日志 ~/logs/learn_ingest.log 可查），不影响学习主流程。
    """
    try:
        import subprocess
        log = open(Path.home() / "logs" / "learn_ingest.log", "a")
        cmd = (f'"{RAG_INGEST_PY}" "{RAG_INGEST_SCRIPT}" "{card_path}" '
               f'&& curl -s -X POST {RAG_REFRESH_URL} >/dev/null')
        subprocess.Popen(["/bin/bash", "-c", cmd],
                         stdout=log, stderr=log, start_new_session=True)
    except Exception:
        pass


# ============ 1. LLM 档案生成 ============

def _gen_concept_profile(concept: str) -> dict | None:
    """LLM 生成概念档案 JSON, 失败返回 None"""
    try:
        ok, text = llm_client.call_by_task(
            "structured",
            f"请生成概念「{concept}」的学习档案",
            system_prompt=PROFILE_PROMPT.format(concept=concept),
            timeout=LLM_TIMEOUT,
        )
    except Exception:
        return None
    if not ok or not text:
        return None
    try:
        profile = _parse_llm_json(text)
    except Exception:
        return None
    return profile if isinstance(profile, dict) and profile.get("concept") else None


def _parse_llm_json(text: str) -> dict:
    """容错解析 LLM 返回的 JSON（剥 markdown 围栏 / 截取 {} 区间）"""
    clean = text.strip()
    for prefix in ["```json\n", "```json", "```\n", "```"]:
        if clean.startswith(prefix):
            clean = clean[len(prefix):]
            break
    for suffix in ["\n```", "```"]:
        if clean.endswith(suffix):
            clean = clean[:-len(suffix)]
            break
    clean = clean.strip()
    if not clean.startswith("{"):
        start, end = clean.find("{"), clean.rfind("}")
        if start >= 0 and end > start:
            clean = clean[start:end + 1]
    return json.loads(clean)


# ============ 2. 本地锚定 ============

def _anchor_local(concept: str, aliases) -> tuple:
    """RAG :8765 检索 + papers.db 只读标题查询, 各自失败降级为空"""
    rag_hits, papers = [], []

    try:
        from core.rag_client import get_rag_client
        rag_hits = (get_rag_client().search(concept, top_k=5) or [])[:5]
    except Exception:
        pass

    try:
        terms = [concept] + [str(a) for a in (aliases or [])]
        conn = sqlite3.connect(f'file:{PAPERS_DB}?mode=ro', uri=True)
        cur = conn.cursor()
        seen = set()
        for term in terms[:3]:
            term = term.strip()
            if len(term) < 2:
                continue
            # instr 替代 LIKE, 避免概念名含 % _ 时的通配符歧义
            for pid, title, aid in cur.execute(
                    "SELECT id, title, arxiv_id FROM papers "
                    "WHERE instr(lower(title), lower(?)) > 0 LIMIT 5", (term,)):
                if pid not in seen:
                    seen.add(pid)
                    papers.append({"id": pid, "title": title, "arxiv_id": aid})
        conn.close()
        papers = papers[:5]
    except Exception:
        pass

    return rag_hits, papers


# ============ 3. 卡片渲染与写入 ============

def _render_card(profile: dict, rag_hits: list, papers: list) -> str:
    """渲染 Obsidian 概念卡片 (frontmatter + 双链)"""
    name = profile["concept"]
    today = _today()
    aliases = [str(a) for a in (profile.get("aliases") or [])]
    upstream = [str(u) for u in (profile.get("upstream") or [])]
    family = upstream[0] if upstream else ""
    conf = profile.get("confidence", "medium")
    status = "unverified" if conf == "low" else "learned"

    fm_lines = [
        "---",
        "type: concept",
        "aliases: [" + ", ".join(aliases) + "]" if aliases else "aliases: []",
        f"family: {family}",
        f"status: {status}",
        f"confidence: {conf}",
        f"created: {today}",
        f"updated: {today}",
        "---",
    ]

    parts = ["\n".join(fm_lines), f"\n# {name}\n"]
    if conf == "low":
        parts.append("> ⚠️ 本卡片 confidence=low，内容可能不准确，请人工核实后使用。\n")
    if profile.get("one_liner"):
        parts.append(f"> {profile['one_liner']}\n")
    if profile.get("definition"):
        parts.append(f"## 定义\n\n{profile['definition']}\n")

    # 家族位置
    chain = " > ".join(f"[[{c}]]" for c in (profile.get("domain_chain") or []))
    tree = profile.get("family_tree") or {}
    evolution = " → ".join(str(c) for c in (tree.get("chain") or []))
    if chain or evolution:
        sec = "## 家族位置\n"
        if chain:
            sec += f"\n领域链: {chain}\n"
        if evolution:
            sec += f"\n演进: {evolution}\n"
        if tree.get("note"):
            sec += f"\n> {tree['note']}\n"
        parts.append(sec)

    # 横向对比
    table = _render_comparison(profile.get("comparison"))
    if table:
        parts.append(f"## 横向对比\n\n{table}\n")

    # 同族概念
    peers = [p for p in (profile.get("peers") or []) if p.get("name")]
    if peers:
        lines = ["## 同族概念\n"]
        for p in peers:
            diff = f" — {p['diff']}" if p.get("diff") else ""
            lines.append(f"- [[{p['name']}]]{diff}")
        parts.append("\n".join(lines) + "\n")

    # 时间线
    timeline = [t for t in (profile.get("timeline") or []) if t.get("event")]
    if timeline:
        lines = ["## 发展时间线\n"]
        for t in timeline:
            lines.append(f"- {t.get('year', '?')}: {t['event']}")
        parts.append("\n".join(lines) + "\n")

    # 关键出处
    refs = [r for r in (profile.get("key_refs") or []) if r.get("title")]
    if refs:
        lines = ["## 关键出处\n"]
        for r in refs:
            note = f" — {r['note']}" if r.get("note") else ""
            lines.append(f"- [{r.get('type', 'ref')}] 《{r['title']}》{note}")
        parts.append("\n".join(lines) + "\n")

    # 本地资源
    local_sec = _render_local(rag_hits, papers)
    if local_sec:
        parts.append(local_sec)

    # 关联概念（双链）
    related = [str(r) for r in (profile.get("related_concepts") or [])]
    if related:
        parts.append("## 关联概念\n\n" + " · ".join(f"[[{r}]]" for r in related) + "\n")

    parts.append(f"\n---\n*由 /learn 生成于 {_now()}*\n")
    return "\n".join(parts)


def _render_comparison(rows) -> str:
    """comparison JSON → Markdown 表格, 列 = 本概念 + 同族概念"""
    rows = [r for r in (rows or []) if r.get("aspect") and isinstance(r.get("values"), dict)]
    if not rows:
        return ""
    cols = []
    for r in rows:
        for k in r["values"]:
            if k not in cols:
                cols.append(k)
    if not cols:
        return ""
    lines = ["| 维度 | " + " | ".join(cols) + " |",
             "|" + "---|" * (len(cols) + 1)]
    for r in rows:
        cells = " | ".join(
            str(r["values"].get(c, "—")).replace("|", "\\|").replace("\n", " ")
            for c in cols)
        lines.append(f"| {r['aspect']} | {cells} |")
    return "\n".join(lines)


def _render_local(rag_hits: list, papers: list) -> str:
    """本地资源区: RAG 命中 + 库内论文"""
    if not rag_hits and not papers:
        return ""
    lines = ["## 本地资源\n"]
    if rag_hits:
        lines.append("### RAG 命中\n")
        for h in rag_hits:
            src = str(h.get("source") or "unknown")
            score = h.get("score")
            score_s = f"{score:.2f}" if isinstance(score, (int, float)) else "?"
            snippet = re.sub(r"\s+", " ", str(h.get("text") or ""))[:200]
            lines.append(f"- (score {score_s}) `{src}`: {snippet}")
    if papers:
        lines.append("\n### 库内论文\n")
        for p in papers:
            aid = p.get("arxiv_id") or ""
            link = f" https://arxiv.org/abs/{aid}" if aid else ""
            lines.append(f"- 《{p.get('title', '')}》{link}")
    return "\n".join(lines) + "\n"


def _write_card(name: str, content: str) -> tuple:
    """幂等写卡: 已存在则保留 created、并迁移旧卡的「家族成员」区

    Returns: (是否由 stub 升级, 卡片路径)
    """
    path = _card_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    is_upgrade = False
    if path.exists():
        old = path.read_text(encoding="utf-8")
        is_upgrade = "status: stub" in old
        m = re.search(r"^created: (.+)$", old, re.M)
        if m:
            content = content.replace(f"created: {_today()}",
                                      f"created: {m.group(1).strip()}", 1)
        # 迁移旧卡已积累的家族成员链接（学同族新概念时挂载的）
        members = _extract_section(old, "家族成员")
        if members and "## 家族成员" not in content:
            content += f"\n## 家族成员\n\n{members}\n"
    path.write_text(content, encoding="utf-8")
    return is_upgrade, path


def _extract_section(text: str, header: str) -> str:
    """提取 ## header 区正文（到下一个 ## 或文件尾）"""
    m = re.search(rf"^## {re.escape(header)}\s*\n(.*?)(?=^## |\Z)",
                  text, re.M | re.S)
    return m.group(1).strip() if m else ""


# ============ 4. 图谱生长 ============

def _grow_graph(profile: dict) -> list:
    """stub 占位卡 + 上位卡家族成员挂载 + concepts.json 关系索引

    confidence=low 时不生长图谱（防幻觉内容污染知识网络），仅登记概念本身。
    Returns: 新建 stub 的概念名列表
    """
    name = profile["concept"]
    if profile.get("confidence") == "low":
        _update_index(profile, [])
        return []
    upstream = [str(u).strip() for u in (profile.get("upstream") or []) if str(u).strip()]
    peers = [str(p.get("name", "")).strip() for p in (profile.get("peers") or [])]
    related = [str(r).strip() for r in (profile.get("related_concepts") or [])]

    stub_candidates = (upstream[:MAX_STUB_UPSTREAM]
                       + [p for p in peers if p][:MAX_STUB_PEERS]
                       + [r for r in related if r][:MAX_STUB_RELATED])
    new_stubs = []
    for c in stub_candidates:
        if c and c != name and _write_stub(c, source=name):
            new_stubs.append(c)

    # 把本概念挂到上位卡的「家族成员」区
    for up in upstream:
        _append_family_member(up, name)
    # 反向认领: 已有卡片的 family 指向本概念时, 挂入本卡的家族成员区
    _claim_existing_members(name)

    _update_index(profile, new_stubs)
    return new_stubs


def _claim_existing_members(name: str):
    """反向认领家族成员（如学「优化器」时认领 family=优化器 的 Muon 卡）"""
    if not VAULT_CONCEPTS.exists():
        return
    for f in VAULT_CONCEPTS.glob("*.md"):
        if f.stem == name:
            continue
        try:
            head = f.read_text(encoding="utf-8")[:600]
        except Exception:
            continue
        m = re.search(r"^family:\s*(.+)$", head, re.M)
        if m and m.group(1).strip() == name:
            _append_family_member(name, f.stem)


def _write_stub(concept: str, source: str) -> bool:
    """创建 stub 占位卡, 已存在（无论 stub/learned）则跳过"""
    path = _card_path(concept)
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"""---
type: concept
status: stub
created: {_today()}
---

# {concept}

> 占位卡片（stub）—— 学习 [[{source}]] 时自动创建，发送 `/learn {concept}` 可填充内容。

## 家族成员

## 被引用
- [[{source}]]
""", encoding="utf-8")
    return True


def _append_family_member(upstream: str, member: str):
    """把成员链接挂到上位概念卡的「家族成员」区（无此区则补建）"""
    path = _card_path(upstream)
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    link = f"- [[{member}]]"
    if link in text:
        return
    if "## 家族成员" in text:
        text = re.sub(r"(^## 家族成员\s*\n)",
                      rf"\1\n{link}\n", text, count=1, flags=re.M)
    else:
        text = text.rstrip() + f"\n\n## 家族成员\n\n{link}\n"
    path.write_text(text, encoding="utf-8")


def _update_index(profile: dict, new_stubs: list):
    """结构化关系索引: 概念节点 + upstream/peer/related 边（供二期 compare/map）"""
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    idx = {"concepts": {}, "edges": []}
    if INDEX_PATH.exists():
        try:
            idx = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    idx.setdefault("concepts", {})
    idx.setdefault("edges", [])

    name = profile["concept"]
    old = idx["concepts"].get(name, {})
    idx["concepts"][name] = {
        "status": "learned",
        "confidence": profile.get("confidence", "medium"),
        "aliases": profile.get("aliases", []),
        "upstream": profile.get("upstream", []),
        "created": old.get("created") or _today(),
        "updated": _today(),
    }
    for stub in new_stubs:
        idx["concepts"].setdefault(
            stub, {"status": "stub", "created": _today(), "updated": _today()})

    def add_edge(frm, to, typ):
        if not to or to == frm:
            return
        e = {"from": frm, "to": to, "type": typ}
        if e not in idx["edges"]:
            idx["edges"].append(e)

    # confidence=low 的概念不挂边，避免幻觉关系进入索引
    if profile.get("confidence") != "low":
        for up in (profile.get("upstream") or []):
            add_edge(name, str(up), "upstream")
        for p in (profile.get("peers") or []):
            add_edge(name, str(p.get("name", "")), "peer")
        for r in (profile.get("related_concepts") or []):
            add_edge(name, str(r), "related")

    INDEX_PATH.write_text(json.dumps(idx, ensure_ascii=False, indent=1),
                          encoding="utf-8")


# ============ 工具 ============

def _safe_filename(name: str) -> str:
    """概念名 → 文件名（Obsidian 对中文/空格友好, 只需防路径分隔符）"""
    return re.sub(r"[/\\]", "-", name).strip() or "unnamed"


def _card_path(concept: str) -> Path:
    return VAULT_CONCEPTS / f"{_safe_filename(concept)}.md"


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M")
