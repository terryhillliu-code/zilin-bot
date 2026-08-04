# 知微系统操作手册 (HANDOFF.md)
> 供宿主机 Claude Code 维护使用 | 最后更新: 2026-03-27

## 1. 系统架构速查
> ⚠️ **深度细节(AI 必读)**: 见 [SYSTEM_DEEP_DIVE.md](file:///Users/liufang/SYSTEM_DEEP_DIVE.md) 和 [AI_IGNITION_PROMPT.md](file:///Users/liufang/AI_IGNITION_PROMPT.md)

```
用户(飞书) ─── ws_client.py(Bot) ─── command_handler.py
                    │                        │
                    │                   ┌────┴────┐
                    │              tasks.db    messages.db
                    │                   │         │
               MessageBus ◄────── worker.py(Dev) │
                    │                   │         │
                    │              clawdbot(Docker)│
                    ▼                             │
              scheduler.py ◄──────────────────────┘
```

| 模块 | 路径 | 虚拟环境 | 服务名 |
|:---|:---|:---|:---|
| **Bot** (消息网关) | `~/zhiwei-bot/` | `~/zhiwei-bot/venv/` | `com.zhiwei.bot` |
| **Dev** (任务执行) | `~/zhiwei-dev/` | 系统 python3 | `com.zhiwei.dev-worker` |
| **Scheduler** (定时调度) | `~/zhiwei-scheduler/` | `~/zhiwei-scheduler/venv/` | `com.zhiwei.scheduler` |
| **RAG** (知识检索) | `~/zhiwei-rag/` | `~/zhiwei-rag/venv/` | `com.zhiwei.rag-api` |
| **Common** (共享库) | `~/zhiwei-common/` | 各 venv 均以 `-e` 安装 | — |
| **Docker** (AI 引擎) | `clawdbot` 容器 | — | Docker |

## 2. 关键密钥

所有密钥集中存储于 `~/.secrets/global.env`，通过 `zhiwei_common.load_secrets()` 加载。
**绝不要**在代码中硬编码 API Key。

## 3. 常用运维命令

### 服务管理
```bash
# 查看所有服务状态
launchctl list | grep zhiwei

# 重启单个服务
launchctl kickstart -k gui/$(id -u)/com.zhiwei.bot
launchctl kickstart -k gui/$(id -u)/com.zhiwei.dev-worker
launchctl kickstart -k gui/$(id -u)/com.zhiwei.scheduler

# 停止/启动
launchctl bootout gui/$(id -u)/com.zhiwei.bot
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.zhiwei.bot.plist

# Docker 容器
docker restart clawdbot
docker exec -it clawdbot bash
```

### 日志查看
```bash
# 实时跟踪
tail -f ~/logs/zhiwei-bot.log
tail -f ~/logs/dev-worker.log
tail -f ~/logs/zhiwei-bot.error.log

# 搜索错误
grep -i "error\|exception\|traceback" ~/logs/zhiwei-bot.error.log | tail -n 20
```

### 数据库操作
```bash
# 查看任务队列
sqlite3 ~/zhiwei-dev/tasks.db "SELECT id,status,substr(input,1,50),created_at FROM tasks ORDER BY id DESC LIMIT 10"

# 查看消息队列
sqlite3 ~/zhiwei-dev/messages.db "SELECT id,topic,status,substr(content,1,50) FROM messages ORDER BY id DESC LIMIT 10"

# 向量库状态
curl -s http://127.0.0.1:8765/status | python3 -m json.tool
```

### 依赖管理
```bash
# 安装/更新 zhiwei-common 到各 venv（修改 common 后必须执行）
~/zhiwei-bot/venv/bin/pip install -e ~/zhiwei-common
~/zhiwei-scheduler/venv/bin/pip install -e ~/zhiwei-common

# 清除 pyc 缓存（排查导入问题时使用）
find ~/zhiwei-common ~/zhiwei-bot ~/zhiwei-dev -name "__pycache__" -exec rm -rf {} + 2>/dev/null
```

## 4. 故障排查流程

### Bot 无法启动
1. `tail -n 30 ~/logs/zhiwei-bot.error.log` → 查看错误
2. 常见原因：
   - `ModuleNotFoundError: zhiwei_common` → 执行 `~/zhiwei-bot/venv/bin/pip install -e ~/zhiwei-common`
   - `ModuleNotFoundError: xxx` → 检查 `~/zhiwei-bot/` 下是否存在该 `.py` 文件
3. `launchctl kickstart -k gui/$(id -u)/com.zhiwei.bot` → 重启

### 任务卡死
1. `sqlite3 ~/zhiwei-dev/tasks.db "SELECT * FROM tasks WHERE status='running'"` → 查看卡住的任务
2. 超过 20 分钟的 → `UPDATE tasks SET status='pending' WHERE id=X` → 自动重试
3. `launchctl kickstart -k gui/$(id -u)/com.zhiwei.dev-worker` → 重启 worker

### RAG 搜索无结果
1. `curl -s http://127.0.0.1:8765/status` → 确认服务存活
2. `~/zhiwei-rag/data/lance_db/write.lock` 如果存在 → 删除它
3. `launchctl kickstart -k gui/$(id -u)/com.zhiwei.rag-api` → 重启

### 消息投递失败
1. `sqlite3 ~/zhiwei-dev/messages.db "SELECT * FROM messages WHERE status='pending' LIMIT 5"` → 查看积压
2. Bot 日志中有 `consume_pending` 异常 → 检查 `zhiwei-common` 是否最新

## 5. 架构约束（必须遵守）

> [!CAUTION]
> 以下为系统红线，违反将导致服务崩溃。

1. **不要直接修改 `zhiwei-dev/` 中的代码**，所有修改通过 `/dev` 指令由 worker 自动在 worktree 中完成
2. **不要在 Docker 容器外写代码文件**，容器是唯一的"代码写入者"
3. **不要使用 `sys.path.insert`**，所有跨项目依赖通过 `zhiwei-common` 管理
4. **修改 `zhiwei-common` 后必须重装**到所有使用它的 venv
5. **密钥只存 `~/.secrets/global.env`**，不要在 `.env` 文件中分散存储

## 6. 开发工作流

```
[飞书] /dev 修复某个bug
   ↓
[Bot] command_handler 入队 → tasks.db
   ↓
[Worker] claim_next → Docker exec claude -p "..."
   ↓
[Docker] 在 worktree 中修改代码 → git commit
   ↓
[Worker] verify_evidence → 语法检查 + git diff
   ↓
[Worker] 自动合并(低风险) 或 等待 /accept(高风险)
   ↓
[Bot] 通知用户结果
```

## 7. plist 文件位置

所有 launchd 配置位于 `~/Library/LaunchAgents/com.zhiwei.*.plist`。
修改后需要 `launchctl bootout` + `launchctl bootstrap` 才能生效。

## 8. 待归档：根目录 chat_handler.py（2026-08-04）

根目录 `chat_handler.py` 在飞书文本路径上未被使用（`command_handler.py` 用的是
`commands/chat_handler.py` 的 `ChatHandler`）。标记待归档，勿再扩展。
