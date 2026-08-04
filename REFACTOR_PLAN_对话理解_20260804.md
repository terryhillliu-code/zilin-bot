# 知微 bot 对话理解能力重构 — 实施文档（供 Claude Code 执行）

> 生成日期：2026-08-04。基于当日真实失败对话（`~/zhiwei-dev/message_log.db`）与代码实机核查。
> 执行者须知：本文档自包含，所有「现状」代码片段均摘自当前仓库，行号可能与最新工作区略有漂移，以符号名为准。

---

## 0. 执行者硬性约束（先读）

1. **受保护文件，全程禁止修改**（PreToolUse hook 会拦截，且本方案已刻意绕开）：
   - `~/zhiwei-bot/ws_client.py`、`~/zhiwei-bot/llm_client.py`、`~/zhiwei-bot/chat_handler.py`（根目录）、`~/zhiwei-bot/.env`、`~/zhiwei-bot/prompts/`
   - 注意：要改的是 `~/zhiwei-bot/commands/chat_handler.py`，**不是**根目录那个同名文件。
2. **LLM 合规**：所有 LLM 调用只走 `zhiwei_common.llm`（默认 Coding Plan），禁止新增 token_plan/prefer_api 参数、禁止直连降级。
3. **SQLite 红线**：新增 DB 写入必须 WAL 模式 + 绝对路径，仿照 `~/zhiwei-common/zhiwei_common/task_store.py` L96-99 的 `_connect()` 模式。
4. **不动**：lance_db 索引、zhiwei-scheduler jobs、feishu_note_sync 逻辑、MessageBus（messages.db）。
5. 每阶段一个 git commit，可独立回滚。P1 的 store 接入加环境变量开关 `CONV_STORE=0` 可退回旧路径。
6. 测试涉及 job/metrics 时 patch `log_task_metrics` 防污染。

---

## 1. 问题定界

2026-08-04 上午用户与 bot 的真实对话（入站记录见 `~/zhiwei-dev/message_log.db`）：

| 时间 | 用户消息 | bot 实际行为 | 问题 |
|---|---|---|---|
| 09:37 | 抖音视频链接 | 媒体管线出笔记 | 正常，但产物未写入任何会话上下文 |
| 10:07-10:08 | 「狮驼岭=美国、凤仙郡=中国…代入重新分析那个视频」 | 走 chat 兜底，LLM 上下文中无视频内容 | 泛泛而谈 |
| 10:11 | 重发链接 + 回复「继续」 | 重复视频提示了「回复『继续』重新处理」，但没有任何代码消费「继续」 | 假承诺，空转 |
| 10:16-10:19 | 连续三次纠正 | 同上，无上下文 | 对话崩坏 |

### 五个根因（均已实机核实）

1. **会话上下文四分五裂**：`ws_client.py` L181-184 `add_to_history` 每条截断 100 字符仅展示用；`zhiwei_common/llm.py` `_session_store`（约 L1058）内存级、重启即失；根目录 `chat_handler.py` 的 MemoryManager 在飞书文本路径上**从未被调用**（`command_handler.py` L150 用的是 `commands/chat_handler.py` 的 ChatHandler）。
2. **媒体产物不回写**：`media_handler.py` `handle_video_async`（L309-321）完成后只 reply，不触碰任何记忆/历史。
3. **「继续」假承诺**：`commands/media_commands.py` L60 提示回复「继续」，但 `pending_video_confirm`（ws_client L96 声明、L211 注入 ctx）在 `commands/` 下**零消费者**；且 L53-61 发现重复时连登记都没做。
4. **OpenClaw 死路径**：`commands/chat_handler.py` L127 每轮先 `docker exec clawdbot ...`，Docker daemon 已全停（实测 `Cannot connect to the Docker daemon`），必失败后走 L129-139 降级。
5. **意图体系缺「媒体追问」**：`commands/nl_router.py` INTENT_PROMPT（L14-39）仅 6 类意图，回指型追问全部掉进 chat 兜底。

### 验收场景（全部通过才算完成）

| # | 场景 | 预期 |
|---|---|---|
| A | 贴视频→笔记生成后追问「那个视频里 X 是什么意思」 | 基于该视频转写/笔记回答 |
| B | 贴视频→发代称映射→要求「代入重新分析」 | 带指令重跑蒸馏（复用转写缓存）→输出还原后摘要 |
| C | 重复贴同一链接后回复「继续」 | 真正重处理 |
| D | 闲聊、/help、knowledge_query、learn_concept、capture、图片追问、语音确认 | 不回归 |
| E | `launchctl kickstart -k` 重启 bot 后再追问 | 仍能找到最近媒体产物（持久化） |
| F | 兜底对话延迟 | 去掉 OpenClaw 失败调用后明显下降 |

---

## 2. P0 止血（commit 1）

### P0.1 删除 OpenClaw 死分支 — `commands/chat_handler.py`

现状 L126-139：
```python
        # ⭐ 优先使用 OpenClaw（有 session 记忆）
        success, response = _call_openclaw_agent(enriched_message, session_id, agent="main")

        if not success:
            # 降级：使用本地 LLM + memory_manager
            logger.warning(f"OpenClaw 调用失败: {response}，降级到本地 LLM")
            from zhiwei_common.llm import llm_client
            ...
            response = llm_client.call_with_session("chat", full_message, session_id)
```

改为：删除 `_call_openclaw_agent` 调用与 success 判断，直接执行原降级分支（`from zhiwei_common.llm import llm_client` … `call_with_session`）。`_call_openclaw_agent` 函数体（L31-76）保留，在 docstring 首行加 `# DEPRECATED 2026-08-04: Docker/clawdbot 已退役，勿再接线` 注释。同步删除文件顶部 `OPENCLAW_CONTAINER = "clawdbot"` 的使用点之外的引用（常量本身可留）。

注意 L146-161 `_save_memories_bg` 中 `memory` 变量原仅在降级分支定义，改后该分支成为主路径，确保 `memory = self.get_memory(user_id)` 仍然定义（原降级分支代码整体保留即可）。

### P0.2 媒体产物回写会话（过渡实现）— `media_handler.py`

`handle_video_async`（L309-321）现状：
```python
def handle_video_async(text: str, message_id: str, user_id: str):
    def _process():
        try:
            response = process_video(text, message_id)
            reply_message(message_id, response)
            TaskLogger.log_task("视频分析", "完成", extract_video_url(text))
```

在 `reply_message` 后追加（成功判定：response 以 "✅" 开头）：
```python
            if response.startswith("✅"):
                try:
                    from zhiwei_common.llm import llm_client
                    llm_client._append_session(
                        f"feishu-{user_id}",
                        f"[系统通知] 用户刚才发的视频已分析完成。结果摘要：{response[:600]}",
                        "已记录，后续追问可基于以上内容回答")
                except Exception as e:
                    logger.warning(f"视频产物注入会话失败: {e}")
```

这是 P1.1 ConversationStore 上线前的过渡手段，P1.1 完成后删除。

### P0.3 接通「继续」重处理 — `commands/media_commands.py` + `command_handler.py`

1. `media_commands.py` L53-61：dup 命中时，在 reply 之前登记（文件顶部已 import 足够模块，需 `import time`）：
```python
            dup = _video_history.check_duplicate(url)
            if dup:
                ctx.pending_video_confirm[user_id] = {
                    "url": url, "text": text_stripped,
                    "message_id": message_id, "time": time.time(),
                }
                reply_message(...)  # 原有提示不变
                return True
```
（注意 ctx 注入名：`init_command_handler` 的 arg_names 含 `"pending_video_confirm"`，见 `command_handler.py` L47。）

2. `command_handler.py`：在 pending_image 块（L85-105）之后、`try:` 命令链（L107）之前插入：
```python
    # 视频重复确认消费（2026-08-04 P0.3：原 pending_video_confirm 只写不读）
    _pvc = getattr(_ctx, 'pending_video_confirm', None)
    if _pvc is not None and user_id in _pvc:
        _entry = _pvc[user_id]
        if time.time() - _entry.get("time", 0) > 600:
            _pvc.pop(user_id, None)
        elif text_lower in ("继续", "重新处理"):
            _pvc.pop(user_id, None)
            _ctx.reply_message(message_id, "🎬 好的，重新处理该视频...")
            return _ctx.handle_video_async(_entry["text"], message_id, user_id)
        elif text_lower in ("取消", "算了"):
            _pvc.pop(user_id, None)
            _ctx.reply_message(message_id, "已取消，不再重复处理。")
            return
```

P0 完成后 commit：`P0: 接通话视频重复确认通道，删除 OpenClaw 死分支，媒体产物注入会话`。重启验证：`launchctl kickstart -k gui/$(id -u)/com.zhiwei.bot`。

---

## 3. P1 结构重构（commit 2-4）

### P1.1 新建 `core/conversation_store.py`

**存储**：`~/zhiwei-dev/conversation.db`（绝对路径，经 `zhiwei_common.config` 拼路径，禁相对路径）。连接仿 `zhiwei_common/task_store.py` L96-99：
```python
@contextmanager
def _connect(self):
    conn = sqlite3.connect(str(self._db_path), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
```

**Schema**：
```sql
CREATE TABLE IF NOT EXISTS turns (
  id INTEGER PRIMARY KEY, user_id TEXT NOT NULL,
  role TEXT NOT NULL,            -- user / assistant / system
  content TEXT NOT NULL,         -- 完整内容，不截断
  kind TEXT DEFAULT 'chat',      -- chat / artifact_notice / instruction
  created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS artifacts (
  id INTEGER PRIMARY KEY, user_id TEXT NOT NULL,
  kind TEXT NOT NULL,            -- video / article / podcast / image
  url TEXT, title TEXT,
  note_path TEXT,
  summary TEXT,                  -- ≤500字，注入上下文用
  instruction TEXT,              -- 最近一次用户注入的指令（如代称映射）
  created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_turns_user ON turns(user_id, id);
CREATE INDEX IF NOT EXISTS idx_artifacts_user ON artifacts(user_id, id);
```

**API 契约**：

| 方法 | 输入 | 输出/副作用 |
|---|---|---|
| `record_turn(user_id, role, content, kind='chat')` | 消息文本 | 写入；同事务内 `DELETE` 该 user 超出最近 50 轮的旧 turns |
| `register_artifact(user_id, kind, url, title, note_path, summary)` | 产物元数据 | 返回 artifact_id（int） |
| `get_last_artifact(user_id, kind=None)` | — | dict（含全部列）或 None |
| `build_context(user_id, max_chars=3000)` | — | 字符串：`【最近处理的内容】\n标题/类型/摘要/笔记路径\n\n【对话历史】\n用户: ...\n你: ...`（最近 8 轮，整体截到 max_chars）；无数据返回 `""` |
| `set_instruction(artifact_id, instruction)` | 指令原文 | 更新 artifacts.instruction |

模块级单例 `conversation_store = ConversationStore()`。环境变量 `CONV_STORE=0` 时所有方法退化为 no-op/空返回（一键回退开关）。

**接入点**：
- `command_handler.py` 兜底分支（L149-151）：调 ChatHandler 前
  ```python
  from core.conversation_store import conversation_store
  conversation_store.record_turn(user_id, "user", text_stripped)
  _conv_ctx = conversation_store.build_context(user_id)
  _msg = f"{_conv_ctx}\n\n当前消息: {text_stripped}" if _conv_ctx else text_stripped
  handler.handle_chat_message(_msg, user_id, message_id, session_id)
  ```
- `media_handler.py` `handle_video_async` 成功分支：**删除 P0.2 的 `_append_session` 注入**，替换为
  ```python
  conversation_store.register_artifact(user_id, "video", extract_video_url(text), title, output_path, response[:500])
  conversation_store.record_turn(user_id, "system", f"视频《{title}》分析完成：{response[:300]}", kind="artifact_notice")
  ```
  （title/output_path 需从 response 或 process_video 返回值获得——可将 `process_video` 改为返回 `(response, meta_dict)`，或在 `_build_video_digest` 处一并回传，实现者自选，保持向后兼容。）
- `commands/chat_handler.py`：bot 回复后 `conversation_store.record_turn(user_id, "assistant", response)`（放在现有 `add_to_history` 旁，保留原调用不动）。

### P1.2 `commands/nl_router.py` 新增 media_followup 意图

**INTENT_PROMPT** 在意图类型列表追加：
```
- media_followup: 针对「刚才发的视频/文章/播客」的追问、修正或要求重做。
  识别标志：出现"刚才/那个视频/这条链接/重新分析/代入/你没理解"等回指词，且消息中不含新链接。
  action 取 qa（基于已有产物直接问答）或 reanalyze（用户给了新指令/映射，要求重跑管线）。
```
输出格式改为（旧意图可不输出新字段，向后兼容）：
```
{"intent": "...", "confidence": 0.0-1.0, "topic": "...", "action_summary": "...",
 "action": "qa|reanalyze（仅 media_followup）", "instruction": "用户的完整指令原文（仅 media_followup）"}
```
示例追加：
```
用户: 这个博主的狮驼岭是指美国，凤仙郡是指中国，请代入重新分析刚才那个视频
{"intent":"media_followup","confidence":0.92,"action":"reanalyze","instruction":"狮驼岭=美国、凤仙郡=中国、棒子=韩国、鬼子=日本。代入这些真实指向还原代称后，重新输出整个视频的观点和摘要","action_summary":"带代称映射重跑视频分析"}
用户: 刚才那个视频第三点再展开讲讲
{"intent":"media_followup","confidence":0.9,"action":"qa","instruction":"展开讲第三点","action_summary":"基于最近视频笔记回答追问"}
```

**`_parse_intent` 白名单**（L63）加 `"media_followup"`；`action` 仅允许 `qa`/`reanalyze`，非法值置 None。

**`route_natural_language` 新增分支**（放在 learn_concept 分支之后、capture 之前）：
```python
        if kind == "media_followup":
            if conf < 0.5:
                return False
            from core.conversation_store import conversation_store
            artifact = conversation_store.get_last_artifact(user_id)
            if not artifact:
                ctx.reply_message(message_id,
                    "我这边没有找到最近处理过的视频/文章，请把链接重发一次，我重新处理。")
                return True
            instruction = intent.get("instruction") or topic or stripped
            action = intent.get("action") or "qa"
            if conf < 0.75:
                _PENDING[user_id] = {"kind": "media_followup",
                                     "content": {"action": action, "instruction": instruction,
                                                 "artifact_id": artifact["id"]}}
                ctx.reply_message(message_id,
                    f"理解为：基于最近的{ '视频' if artifact['kind']=='video' else '内容' }《{artifact.get('title','')[:30]}》，"
                    f"{'按你的指令重新分析' if action=='reanalyze' else '回答你的追问'}。回复「确认」执行。")
                return True
            return _exec_media_followup(action, artifact, instruction, user_id, message_id, ctx)
```

**新增 `_exec_media_followup`**：
- `qa`：读 `artifact["note_path"]` 全文截 4000 字（文件缺失则用 summary），构造
  `f"【背景：{artifact['kind']}《{artifact['title']}》的笔记】\n{note}\n\n【用户追问】{instruction}"`
  → `llm_client.call_with_session("chat", msg, f"feishu-{user_id}")` → reply；并 `record_turn` 回写问答对。
- `reanalyze`：`conversation_store.set_instruction(artifact["id"], instruction)` → 调 `media_handler.reprocess_with_instruction(...)`（见 P1.3）→ reply 回执「已带你的指令重新分析，约 3-5 分钟」。
- `_PENDING` 确认通道的 confirm 分支（L87-98）补 `"media_followup"` kind 的执行。

### P1.3 蒸馏管线指令注入 — `scripts/douyin_distiller.py` + `media_handler.py`

`douyin_distiller.py`（非受保护）：
1. argparse 新增 `--instruction-file PATH`。
2. 主流程中读取该文件内容，包成段落：
   ```
   **用户指定背景/还原指令（最高优先级）：**
   {instruction}
   请在分析与摘要中严格执行上述还原与映射。
   ```
   追加到 `distill()` 已有的 `extra_context` 参数（L3139，与 --vision 视觉信息同通道，会附入 Stage 2 prompt 的 background_section）。实现时定位 `distill(` 调用点，把该段落与现有 extra_context 字符串拼接即可。

`media_handler.py`：
1. `process_video(text, message_id=None, instruction=None)`：instruction 非空时写入 `~/zhiwei-bot/tmp/instruction_{user_id}.txt`（需把 user_id 也透传进来，或改签名 `process_video(text, message_id=None, user_id=None, instruction=None)`，同步更新调用点），cmd 追加 `["--instruction-file", tmp_path]`。
2. 新增：
   ```python
   def reprocess_with_instruction(user_id, artifact, instruction, message_id):
       """带用户指令重跑媒体管线（复用 distiller 转写缓存，不重新下载）"""
       text = f"{artifact['url']} 重新分析"  # 含链接即可走原 extract 逻辑
       handle_video_async(text, message_id, user_id, instruction=instruction)
   ```
   `handle_video_async` 相应加 `instruction=None` 透传。

### P1.4 死代码标注
不改受保护的根目录 `chat_handler.py`。在 `~/zhiwei-bot/HANDOFF.md` 末尾追加一节：「2026-08-04：根目录 chat_handler.py 在飞书文本路径上未被使用（command_handler 用的是 commands/chat_handler.py），标记待归档，勿再扩展。」

---

## 4. P2 增强（commit 5）

1. `douyin_distiller.py` STAGE2 通用 prompt（STAGE2_PROMPTS 的 general 模板，及 SYSTEM_PROMPT L2925 附近的输出契约处）追加固定段落：
   > 若转写中出现明显的代称、借代、规避敏感词的隐晦表达（如地名/绰号代指国家或实体），请在笔记中单列「代称还原」小节，给出每个代称推测的真实指向及置信度（高/中/低），并基于还原后的理解输出观点与摘要。
2. nl_router media_followup-qa 分支回答后 `conversation_store.record_turn` 回写问答对（若 P1.2 未含则补上）。

---

## 5. 测试计划

1. **单测**（新建 `tests/test_conversation_store.py`，用 tmp_path 隔离 DB）：
   - record_turn 写入与 50 轮裁剪；register/get_last_artifact；build_context 长度上限与空库返回；`CONV_STORE=0` no-op。patch `log_task_metrics`。
2. **集成回放**（新建 `tests/test_media_followup_replay.py`，mock ctx + mock distiller subprocess）：
   注入消息序列「抖音链接 → 代称映射+重新分析 → 继续」，断言：
   - media_followup 意图命中且 action=reanalyze；
   - distiller 被调用时 cmd 含 `--instruction-file`，文件内容含映射表；
   - 重复链接场景「继续」触发 `handle_video_async`。
3. **回归**：/help、knowledge_query、learn_concept（高/中置信）、capture、图片追问、语音确认各 mock 一遍。
4. **端到端**（人工）：
   ```bash
   launchctl kickstart -k gui/$(id -u)/com.zhiwei.bot
   ```
   在「知微测试群」实发：视频链接 → 等笔记 → 追问 → 发代称映射要求重析 → 核对输出含还原后观点；再 kickstart 一次，追问验证持久化（验收 E）。
5. **合规核查**：`grep -rn "prefer_api\|token_plan" ~/zhiwei-bot/commands/nl_router.py ~/zhiwei-bot/media_handler.py ~/zhiwei-bot/core/conversation_store.py` 应为 0 命中。

## 6. 回滚

- P0/P1/P2 各一个 commit，`git revert` 即可。
- 运行时开关：`CONV_STORE=0`（写入 bot 的 launchd plist 环境变量或 .env 均可，但 .env 受保护——用 plist 的 EnvironmentVariables 或默认代码内读取 os.environ，部署时由人工加入）。

## 7. 参考坐标速查

| 位置 | 内容 |
|---|---|
| `command_handler.py` L59-156 | 文本消息主链路 handle_text_async |
| `command_handler.py` L39-48 | ctx 注入参数表（含 pending_video_confirm） |
| `commands/chat_handler.py` L31-76, L86-178 | OpenClaw 死分支与兜底对话 |
| `commands/media_commands.py` L51-61 | 重复视频检测（假承诺现场） |
| `commands/nl_router.py` L14-39, L52-68, L76-186 | 意图 prompt / 解析白名单 / 路由主函数 |
| `media_handler.py` L309-321, L401-556 | 异步视频处理 / process_video |
| `scripts/douyin_distiller.py` L2925, L3139 | SYSTEM_PROMPT / distill(extra_context) 注入口 |
| `zhiwei_common/llm.py` ~L1058-1095 | _session_store / call_with_session / _append_session |
| `zhiwei_common/task_store.py` L96-99 | WAL 连接范式 |
