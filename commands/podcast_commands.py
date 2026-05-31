"""
播客管理命令处理器 ⭐
提供 /podcast 命令管理播客订阅列表
"""
import os
import sys
import yaml
import feedparser
import tempfile
import shutil
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, Optional

# 配置文件路径
SETTINGS_PATH = Path.home() / "zhiwei-scheduler" / "config" / "settings.yaml"


def _load_config() -> Dict[str, Any]:
    """加载配置文件"""
    with open(SETTINGS_PATH, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f) or {}


def _save_config(config: Dict[str, Any]) -> None:
    """原子写入配置文件，避免损坏"""
    # 先写入临时文件
    temp_file = tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False, encoding='utf-8')
    try:
        yaml.dump(config, temp_file, allow_unicode=True, sort_keys=False)
        temp_file.close()
        # 替换原文件
        shutil.move(temp_file.name, SETTINGS_PATH)
        return True
    except Exception as e:
        # 出错删除临时文件
        os.unlink(temp_file.name)
        raise e


def _backup_config():
    """备份配置文件"""
    backup_path = SETTINGS_PATH.with_suffix(f".yaml.bak.{datetime.now().strftime('%Y%m%d%H%M%S')}")
    shutil.copy(SETTINGS_PATH, backup_path)
    return backup_path


def _check_rss_valid(rss_url):
    """检查RSS地址是否有效，是否包含音频"""
    try:
        feed = feedparser.parse(rss_url)
        if feed.bozo != 0:
            return False, f"RSS解析失败: {feed.bozo_exception}"

        if not feed.entries:
            return False, "RSS中没有节目内容"

        # 检查最新的条目是否有音频
        latest_entry = feed.entries[0]
        has_audio = False
        if hasattr(latest_entry, 'enclosures') and latest_entry.enclosures:
            for enc in latest_entry.enclosures:
                if hasattr(enc, 'type') and 'audio' in enc.type:
                    has_audio = True
                    break
        # 降级检查：链接是否直接指向音频文件
        elif hasattr(latest_entry, 'link') and latest_entry.link.endswith(('.mp3', '.m4a', '.mp4')):
            has_audio = True

        if not has_audio:
            return False, "RSS中没有找到音频资源，可能不是播客RSS"

        return True, f"✅ RSS有效，共 {len(feed.entries)} 个节目，最新: {latest_entry.title[:30]}..."
    except Exception as e:
        return False, f"检查失败: {str(e)}"


def handle_podcast_commands(text_lower, text_stripped, user_id, message_id, ctx):
    """
    处理 /podcast 命令

    用法：
    - /podcast add <名称> <RSS地址>  - 添加新的播客订阅
    - /podcast list                  - 列出所有订阅的播客
    - /podcast remove <名称>         - 删除指定的播客订阅
    - /podcast check <RSS地址>       - 校验RSS地址是否有效
    - /podcast sync                  - 立即触发播客更新检查
    - /podcast help                  - 帮助信息
    """
    # 只处理 /podcast 开头的命令
    if not text_lower.startswith("/podcast"):
        return False

    # 解析命令
    parts = text_stripped.split(maxsplit=3)
    if len(parts) < 2:
        ctx.reply_message(message_id, _get_help_text())
        return True

    subcommand = parts[1].lower()
    args = parts[2:] if len(parts) > 2 else []

    try:
        # 分发处理
        if subcommand == "add":
            return _handle_add(args, message_id, ctx)
        elif subcommand == "list":
            return _handle_list(message_id, ctx)
        elif subcommand == "remove":
            return _handle_remove(args, message_id, ctx)
        elif subcommand == "check":
            return _handle_check(args, message_id, ctx)
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
    """处理添加播客"""
    if len(args) < 2:
        ctx.reply_message(message_id, "用法: /podcast add <播客名称> <RSS地址>")
        return True

    name = args[0].strip()
    rss_url = args[1].strip()

    ctx.reply_message(message_id, f"正在添加播客「{name}」...\nRSS: {rss_url}")

    # 1. 检查RSS有效性
    valid, msg = _check_rss_valid(rss_url)
    if not valid:
        ctx.reply_message(message_id, f"❌ RSS无效: {msg}")
        return True

    # 2. 加载配置，确保结构完整
    config = _load_config()
    config.setdefault('podcasts', {}).setdefault('feeds', [])

    # 3. 检查是否已存在
    feeds = config['podcasts']['feeds']
    for feed in feeds:
        if feed.get('name', '').lower() == name.lower() or feed.get('url', '') == rss_url:
            ctx.reply_message(message_id, f"⚠️ 该播客已存在：{feed.get('name')}")
            return True

    # 4. 备份配置
    backup_path = _backup_config()

    # 5. 添加新播客
    feeds.append({
        'name': name,
        'url': rss_url
    })

    # 6. 保存配置
    try:
        _save_config(config)
    except Exception as e:
        ctx.reply_message(message_id, f"❌ 保存配置失败: {str(e)}\n已自动回滚，备份文件: {backup_path}")
        return True

    ctx.reply_message(message_id, f"✅ 播客「{name}」添加成功！\n当前共 {len(feeds)} 个订阅\n下次更新时会自动处理该播客的最新节目")
    return True


def _handle_list(message_id, ctx):
    """处理列出播客"""
    config = _load_config()
    feeds = config.get('podcasts', {}).get('feeds', [])

    if not feeds:
        ctx.reply_message(message_id, "当前没有订阅任何播客")
        return True

    lines = [f"📋 当前订阅的播客列表（共 {len(feeds)} 个）："]
    for i, feed in enumerate(feeds, 1):
        name = feed.get('name', '未命名')
        url = feed.get('url', '')
        lines.append(f"{i}. {name}")
        lines.append(f"   RSS: {url[:50]}{'...' if len(url) > 50 else ''}")
        lines.append("")

    ctx.reply_message(message_id, "\n".join(lines))
    return True


def _handle_remove(args, message_id, ctx):
    """处理删除播客"""
    if len(args) < 1:
        ctx.reply_message(message_id, "用法: /podcast remove <播客名称>")
        return True

    name = args[0].strip()

    config = _load_config()
    feeds = config.get('podcasts', {}).get('feeds', [])
    found_index = -1
    found_name = ""
    for i, feed in enumerate(feeds):
        if feed.get('name', '').lower() == name.lower():
            found_index = i
            found_name = feed.get('name')
            break

    if found_index == -1:
        ctx.reply_message(message_id, f"❌ 未找到名为「{name}」的播客")
        return True

    # 备份配置
    backup_path = _backup_config()

    # 删除
    del feeds[found_index]

    # 保存
    try:
        _save_config(config)
    except Exception as e:
        ctx.reply_message(message_id, f"❌ 保存配置失败: {str(e)}\n已自动回滚，备份文件: {backup_path}")
        return True

    ctx.reply_message(message_id, f"✅ 播客「{found_name}」已删除\n当前剩余 {len(feeds)} 个订阅")
    return True


def _handle_check(args, message_id, ctx):
    """处理检查RSS有效性"""
    if len(args) < 1:
        ctx.reply_message(message_id, "用法: /podcast check <RSS地址>")
        return True

    rss_url = args[0].strip()

    ctx.reply_message(message_id, f"正在检查RSS地址: {rss_url}")

    valid, msg = _check_rss_valid(rss_url)
    if valid:
        ctx.reply_message(message_id, f"✅ {msg}")
    else:
        ctx.reply_message(message_id, f"❌ {msg}")
    return True


def _handle_sync(message_id, ctx):
    """处理立即同步播客"""
    ctx.reply_message(message_id, "🚀 正在触发播客更新检查...")

    try:
        # 调用播客更新任务
        result = subprocess.run(
            [sys.executable, "-c", "from scheduler_jobs import job_podcast_update; job_podcast_update()"],
            cwd=str(Path.home() / "zhiwei-scheduler"),
            capture_output=True,
            text=True,
            timeout=300
        )

        if result.returncode == 0:
            ctx.reply_message(message_id, "✅ 播客更新检查完成\n如有新节目会自动推送到飞书")
        else:
            ctx.reply_message(message_id, f"❌ 更新失败: {result.stderr[:200]}")
    except subprocess.TimeoutExpired:
        ctx.reply_message(message_id, "⏰ 更新超时，可能处理时间较长，完成后会自动推送结果")
    except Exception as e:
        ctx.reply_message(message_id, f"❌ 执行失败: {str(e)}")

    return True


def _get_help_text():
    """获取帮助文本"""
    return """📋 播客管理命令帮助

/podcast add <名称> <RSS地址>  添加新的播客订阅
/podcast list                  列出所有订阅的播客
/podcast remove <名称>         删除指定的播客订阅
/podcast check <RSS地址>       校验RSS地址是否有效
/podcast sync                  立即触发播客更新检查
/podcast help                  查看帮助信息

示例：
  /podcast add AI局内人 https://feed.xyzfm.space/jbap8hlxmuev
  /podcast list
  /podcast sync

小宇宙获取RSS方法：播客主页 → 分享 → 复制RSS链接
"""
