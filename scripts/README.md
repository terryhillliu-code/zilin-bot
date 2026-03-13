# 抖音知识蒸馏引擎 使用说明

> 版本: v1.0.0
> 更新日期: 2026-03-13

---

## 一、功能概述

**核心目标**：输入短视频分享链接，自动输出结构化的 Obsidian Markdown 笔记。

**支持平台**：
- 抖音 (douyin.com / v.douyin.com)
- TikTok (tiktok.com)
- B站 (bilibili.com / b23.tv)
- 小红书 (xiaohongshu.com / xhslink.com)
- 快手 (kuaishou.com)
- 微博 (weibo.com / t.cn)

---

## 二、处理流程

```
分享链接 → URL解析 → 视频下载 → 字幕提取 → ASR转录 → LLM蒸馏 → Markdown输出
                         ↓
                   字幕优先策略:
                   1. yt-dlp 探测平台字幕
                   2. 无字幕 → 阿里云 dashscope ASR
                   3. API失败 → 本地 mlx-whisper 兜底
```

---

## 三、安装与配置

### 3.1 依赖已安装

以下依赖已在 `~/zhiwei-bot/venv` 中安装：

| 依赖 | 用途 |
|------|------|
| yt-dlp | 视频下载、字幕探测 |
| dashscope | 阿里云 ASR + LLM |
| mlx-whisper | 本地 ASR 兜底 |
| openai | LLM API 客户端 |

### 3.2 配置自动加载

脚本会自动从以下位置加载配置（优先级从高到低）：

1. `~/zhiwei-bot/.env` — 已有 DASHSCOPE_API_KEY ✅
2. `~/.secrets/zhiwei.env` — 备用配置
3. `scripts/.env` — 脚本目录（可选）

**无需手动配置，可直接使用。**

### 3.3 可选环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `QWEN_MODEL` | qwen-plus | LLM 模型 |
| `ASR_MODEL` | sensevoice-v1 | ASR 模型 |
| `ASR_POLICY` | auto | ASR 策略 (auto/cloud/local) |
| `LOCAL_ASR_MODEL` | small | 本地模型大小 (tiny/small/medium) |
| `OUTPUT_DIR` | ~/Documents/ZhiweiVault/Inbox | 输出目录 |
| `LOG_LEVEL` | INFO | 日志级别 |

---

## 四、使用方法

### 4.1 基本用法

```bash
# 激活虚拟环境
cd ~/zhiwei-bot && source venv/bin/activate

# 运行
python scripts/douyin_distiller.py "https://v.douyin.com/xxx/"
```

### 4.2 命令行参数

| 参数 | 说明 |
|------|------|
| `url` | 视频链接（必填） |
| `--dry-run` | 只解析不生成文件，用于测试 |
| `--transcript-only` | 只输出转录文本，不调用 LLM |
| `--output-dir <path>` | 自定义输出目录 |
| `--debug` | 启用调试模式，输出详细日志 |

### 4.3 使用示例

```bash
# 正常处理
python scripts/douyin_distiller.py "https://v.douyin.com/iFNBfUuM/"

# 测试模式（不生成文件）
python scripts/douyin_distiller.py --dry-run "https://v.douyin.com/iFNBfUuM/"

# 只获取字幕/转录
python scripts/douyin_distiller.py --transcript-only "https://v.douyin.com/iFNBfUuM/"

# 自定义输出目录
python scripts/douyin_distiller.py --output-dir ~/Downloads "https://v.douyin.com/iFNBfUuM/"

# 调试模式
python scripts/douyin_distiller.py --debug "https://v.douyin.com/iFNBfUuM/"
```

---

## 五、输出格式

### 5.1 文件命名

```
{日期}_{标题}.md
例: 2026-03-13_如何高效学习编程.md
```

### 5.2 Markdown 结构

```markdown
---
title: "视频标题"
source_url: "https://v.douyin.com/xxx/"
date: 2026-03-13
tags: [编程, 学习方法, 效率]
type: video_distill
asr_source: "平台字幕"
noise_tags: []
---

# 视频标题

## 💡 一句话核心
核心要点概括（不超过50字）

## 🧠 知识点拆解
- **[00:15]** 第一个知识点内容
- **[01:30]** 第二个知识点内容
- **[03:45]** 第三个知识点内容

## 📝 内容摘要
100-200字的视频内容摘要

## ✅ 行动建议
- 可执行的建议1
- 可执行的建议2

## 🔗 参考资料
- 提到的资源或链接

## 📹 原始信息
- 来源平台：抖音
- 作者：作者名称
- 原始链接：[点击查看](https://...)

---
> 由知微系统自动生成，建议人工审核后归档
```

### 5.3 Frontmatter 字段说明

| 字段 | 说明 |
|------|------|
| `title` | LLM 生成的标题 |
| `source_url` | 原始分享链接 |
| `date` | 处理日期 |
| `tags` | LLM 提取的标签（最多5个） |
| `type` | 固定为 `video_distill` |
| `asr_source` | 转录来源（平台字幕/dashscope_asr/local_asr） |
| `noise_tags` | 检测到的噪音标签（如广告、推销等） |

---

## 六、处理策略详解

### 6.1 字幕优先策略

脚本会依次尝试：

1. **平台字幕** (yt-dlp)：免费、快速、准确
2. **云端 ASR** (dashscope sensevoice)：支持多语言、标点恢复
3. **本地 ASR** (mlx-whisper)：离线兜底，隐私保护

### 6.2 知识蒸馏逻辑

LLM 会提取以下结构化信息：

| 字段 | 说明 | 数量限制 |
|------|------|----------|
| `title` | 简洁标题 | 1个 |
| `one_liner` | 一句话核心 | ≤50字 |
| `key_points` | 知识点（带时间戳） | 3-8个 |
| `summary` | 内容摘要 | 100-200字 |
| `tags` | 标签 | ≤5个 |
| `action_items` | 可执行建议 | 0-N个 |
| `references` | 参考资料 | 0-N个 |

### 6.3 后处理功能

- **文本清洗**：去除重复句、无意义填充词
- **噪音检测**：识别广告、推销、引导关注等内容
- **格式规范化**：统一时间戳格式

---

## 七、常见问题

### Q1: 提示 "DASHSCOPE_API_KEY not set"

检查配置文件是否存在：
```bash
grep DASHSCOPE ~/zhiwei-bot/.env
```

### Q2: 视频下载失败

可能原因：
- 链接已过期，请重新获取分享链接
- 平台限制，尝试使用 `--debug` 查看详细错误

### Q3: ASR 转录为空

可能原因：
- 视频无音频
- 网络问题导致 dashscope 调用失败
- 尝试 `--transcript-only` 查看原始转录

### Q4: LLM 输出格式错误

脚本有降级处理，会返回原始转录文本。

### Q5: 输出目录不存在

脚本会自动创建 `OUTPUT_DIR` 指定的目录。

---

## 八、成本估算

| 服务 | 计费方式 | 预估成本（1分钟视频） |
|------|----------|----------------------|
| 平台字幕 | 免费 | ¥0 |
| dashscope ASR | 按秒计费 | ~¥0.01 |
| dashscope LLM | 按 Token 计费 | ~¥0.02 |
| mlx-whisper | 本地免费 | ¥0（消耗算力） |

**建议**：优先使用平台字幕，成本最低且准确度最高。

---

## 九、扩展与定制

### 9.1 自定义输出模板

编辑 `douyin_distiller.py` 中的 `MarkdownWriter.TEMPLATE`。

### 9.2 自定义 LLM 提示词

编辑 `KnowledgeDistiller.SYSTEM_PROMPT` 和 `USER_PROMPT_TEMPLATE`。

### 9.3 添加新平台支持

在 `URLResolver.PLATFORMS` 字典中添加域名映射。

---

## 十、技术架构

```
douyin_distiller.py (~1027行)
├── 数据模型 (L40-100)
│   ├── VideoInfo
│   ├── TranscriptSegment
│   └── TranscriptResult, DistilledKnowledge
├── 配置管理 (L104-146)
│   └── AppConfig
├── URL解析 (L150-210)
│   └── URLResolver
├── 媒体处理 (L214-330)
│   ├── MediaExtractor
│   └── SubtitleExtractor
├── ASR转录 (L334-490)
│   ├── ASRProvider (抽象基类)
│   ├── DashScopeASRTranscriber
│   └── LocalASRTranscriber
├── 后处理 (L494-560)
│   └── TranscriptPostProcessor
├── 转录协调 (L564-650)
│   └── TranscriptProvider
├── 知识蒸馏 (L654-815)
│   └── KnowledgeDistiller
├── Markdown输出 (L819-915)
│   └── MarkdownWriter
└── CLI入口 (L919-1027)
    └── main()
```

---

## 十一、版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| v1.0.0 | 2026-03-13 | 初始版本，支持主流短视频平台 |

---

## 十二、反馈与改进

如有问题或建议，请在知微系统中提交反馈。

当前已知限制：
1. 部分私密视频无法下载
2. 抖音直播回放暂不支持
3. 超长视频（>30分钟）可能需要调整 LLM 上下文限制