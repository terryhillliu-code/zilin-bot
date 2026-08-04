"""
YouTube 频道订阅命令处理器 ⭐ (v71, 2026-07-31)
提供 /yt 命令管理 YouTube 频道追更列表(免 API,走频道 RSS)

配置写入 zhiwei-scheduler settings.yaml 的 youtube_channels 节,
由 scheduler 的 youtube_update job(每日 19:30)消费。
逻辑复刻 podcast_commands 的备份+校验模式。
"""
import re
import sys
import subprocess
from pathlib import Path

import feedparser
import requests

# 复用播客命令的配置读写(同一 settings.yaml,原子写+备份)
from .podcast_commands import _load_config, _save_config, _backup_config

YT_FEED_TEMPLATE = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"

# v71.1: 本机直连 youtube.com 不通，走阿里云日本 VM 隧道(com.zhiwei.jp-tunnel)
# socks5h: DNS 交给代理端解析
import os
_PROXY = os.getenv("ZHIWEI_VIDEO_PROXY", "socks5://127.0.0.1:18081").replace("socks5://", "socks5h://", 1)
_PROXIES = {"http": _PROXY, "https": _PROXY}


def _resolve_channel_id(raw: str) -> tuple:
    """从输入解析 channel_id。支持:
    - 裸 channel_id (UC 开头 24 位)
    - youtube.com/channel/UCxxx 链接
    - youtube.com/@handle 链接(抓页面提取 channelId)

    返回 (channel_id 或 None, 错误信息)
    """
    raw = raw.strip()

    # 裸 channel_id
    if re.fullmatch(r"UC[\w-]{22}", raw):
        return raw, ""

    # /channel/ 链接
    m = re.search(r"youtube\.com/channel/(UC[\w-]{22})", raw)
    if m:
        return m.group(1), ""

    # @handle 或其他频道链接: 抓页面找 channelId(经日本 VM 代理)
    if "youtube.com/" in raw:
        try:
            url = raw if raw.startswith("http") else f"https://{raw}"
            resp = requests.get(url, timeout=20, proxies=_PROXIES, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
            # ⚠️ 不能拿页面第一个 "channelId"——那可能是推荐内容的。
            # externalId / canonical 才是本频道自身 ID。
            for pattern in (r'"externalId":"(UC[\w-]{22})"',
                            r'rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"',
                            r'"browseId":"(UC[\w-]{22})"'):
                m = re.search(pattern, resp.text)
                if m:
                    return m.group(1), ""
            return None, "页面中未找到频道 ID(可能不是频道页)"
        except Exception as e:
            return None, f"抓取频道页失败(检查 jp-tunnel 隧道): {e}"

    return None, "无法识别的输入,请提供 channel_id(UC开头)或频道 URL"


def _check_feed_valid(channel_id: str) -> tuple:
    """校验频道 RSS 可达且有视频(经日本 VM 代理)

    YouTube RSS 端点偶发 500/404 瞬时限流(实测几秒后恢复)，重试 3 次。
    """
    import time as _time
    last_err = ""
    for attempt in range(3):
        try:
            resp = requests.get(YT_FEED_TEMPLATE.format(cid=channel_id), timeout=20, proxies=_PROXIES)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
            if not feed.entries:
                return False, "RSS 无内容(channel_id 错误或隧道不可用)"
            latest = feed.entries[0]
            return True, f"✅ 频道有效,最新视频: {latest.get('title', '')[:40]}"
        except Exception as e:
            last_err = str(e)
            _time.sleep(5 * (attempt + 1))
    return False, f"RSS 校验失败(检查 jp-tunnel 隧道): {last_err}"


def handle_youtube_commands(text_lower, text_stripped, user_id, message_id, ctx):
    """
    处理 /yt 命令

    用法：
    - /yt add <名称> <channel_id或频道URL>  - 添加频道追更
    - /yt list                              - 列出订阅频道
    - /yt remove <名称>                     - 删除频道订阅
    - /yt sync                              - 立即触发追更检查
    - /yt help                              - 帮助信息
    """
    if not (text_lower.startswith("/yt ") or text_lower.strip() == "/yt"):
        return False

    parts = text_stripped.split(maxsplit=3)
    if len(parts) < 2:
        ctx.reply_message(message_id, _get_help_text())
        return True

    subcommand = parts[1].lower()
    args = parts[2:] if len(parts) > 2 else []

    try:
        if subcommand == "add":
            return _handle_add(args, message_id, ctx)
        elif subcommand == "list":
            return _handle_list(message_id, ctx)
        elif subcommand == "remove":
            return _handle_remove(args, message_id, ctx)
        elif subcommand == "sync":
            return _handle_sync(message_id, ctx)
        elif subcommand == "help":
            ctx.reply_message(message_id, _get_help_text())
            return True
        else:
            ctx.reply_message(message_id, f"未知命令: {subcommand}\n\n{_get_help_text()}")
            return True
    except Exception as e:
        ctx.reply_message(message_id, f"❌ 处理失败: {str(e)}")
        return True


def _handle_add(args, message_id, ctx):
    """添加频道订阅"""
    if len(args) < 2:
        ctx.reply_message(message_id, "用法: /yt add <频道名称> <channel_id或频道URL>")
        return True

    name = args[0].strip()
    raw = args[1].strip()

    ctx.reply_message(message_id, f"正在解析频道「{name}」...")

    channel_id, err = _resolve_channel_id(raw)
    if not channel_id:
        ctx.reply_message(message_id, f"❌ {err}")
        return True

    valid, msg = _check_feed_valid(channel_id)
    if not valid:
        ctx.reply_message(message_id, f"❌ {msg}")
        return True

    config = _load_config()
    config.setdefault('youtube_channels', {}).setdefault('feeds', [])
    feeds = config['youtube_channels']['feeds'] or []

    for feed in feeds:
        if feed.get('name', '').lower() == name.lower() or feed.get('channel_id') == channel_id:
            ctx.reply_message(message_id, f"⚠️ 该频道已存在：{feed.get('name')}")
            return True

    backup_path = _backup_config()
    feeds.append({'name': name, 'channel_id': channel_id})
    config['youtube_channels']['feeds'] = feeds

    try:
        _save_config(config)
    except Exception as e:
        ctx.reply_message(message_id, f"❌ 保存配置失败: {str(e)}\n备份文件: {backup_path}")
        return True

    ctx.reply_message(message_id,
                      f"✅ 频道「{name}」添加成功！\n{msg}\n"
                      f"channel_id: {channel_id}\n"
                      f"当前共 {len(feeds)} 个订阅,每日 19:30 自动追更")
    return True


def _handle_list(message_id, ctx):
    """列出订阅频道"""
    config = _load_config()
    feeds = config.get('youtube_channels', {}).get('feeds', []) or []

    if not feeds:
        ctx.reply_message(message_id, "当前没有订阅任何 YouTube 频道\n用 /yt add <名称> <频道URL> 添加")
        return True

    lines = [f"📺 当前订阅的 YouTube 频道（共 {len(feeds)} 个）："]
    for i, feed in enumerate(feeds, 1):
        lines.append(f"{i}. {feed.get('name', '未命名')} ({feed.get('channel_id', '')})")
    ctx.reply_message(message_id, "\n".join(lines))
    return True


def _handle_remove(args, message_id, ctx):
    """删除频道订阅"""
    if len(args) < 1:
        ctx.reply_message(message_id, "用法: /yt remove <频道名称>")
        return True

    name = args[0].strip()
    config = _load_config()
    feeds = config.get('youtube_channels', {}).get('feeds', []) or []

    found_index = -1
    found_name = ""
    for i, feed in enumerate(feeds):
        if feed.get('name', '').lower() == name.lower():
            found_index = i
            found_name = feed.get('name')
            break

    if found_index == -1:
        ctx.reply_message(message_id, f"❌ 未找到名为「{name}」的频道")
        return True

    backup_path = _backup_config()
    del feeds[found_index]

    try:
        _save_config(config)
    except Exception as e:
        ctx.reply_message(message_id, f"❌ 保存配置失败: {str(e)}\n备份文件: {backup_path}")
        return True

    ctx.reply_message(message_id, f"✅ 频道「{found_name}」已删除\n当前剩余 {len(feeds)} 个订阅")
    return True


def _handle_sync(message_id, ctx):
    """立即触发 YouTube 追更(蒸馏耗时长,后台执行不阻塞)"""
    ctx.reply_message(message_id, "🚀 正在后台触发 YouTube 频道追更...\n蒸馏耗时较长(每视频 3-10 分钟),完成后自动推送简报")

    try:
        scheduler_python = Path.home() / "zhiwei-scheduler" / "venv" / "bin" / "python"
        py = str(scheduler_python) if scheduler_python.exists() else sys.executable
        # 后台执行,不等待(单视频蒸馏可达 30 分钟)
        subprocess.Popen(
            [py, "-c", "from scheduler_jobs import job_youtube_update; job_youtube_update()"],
            cwd=str(Path.home() / "zhiwei-scheduler"),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        ctx.reply_message(message_id, f"❌ 触发失败: {str(e)}")

    return True


def _get_help_text():
    """获取帮助文本"""
    return """📺 YouTube 频道追更命令帮助

/yt add <名称> <channel_id或频道URL>  添加频道追更
/yt list                              列出订阅频道
/yt remove <名称>                     删除频道订阅
/yt sync                              立即触发追更检查
/yt help                              查看帮助信息

示例：
  /yt add Karpathy https://www.youtube.com/@AndrejKarpathy
  /yt add Fireship UCsBjURrPoezykLs9EqgamOA
  /yt list

说明：每日 19:30 自动检查新视频 → 转写蒸馏 → 推送简报;
超过 120 分钟的视频自动跳过。看完视频后可发「链接 + 视觉分析」触发抽帧图表提取。
"""
