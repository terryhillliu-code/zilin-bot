"""
离线消息恢复模块
- 需要权限: im:message.p2p_msg ✅ 已开通
- WebSocket 重连后拉取离线消息
- Phase 3 of 飞书消息离线恢复方案

Created: 2026-03-18
Updated: 2026-03-20 - 移除不存在的 API，改用运行时缓存 chat_id
"""

import time
import json
import lark_oapi as lark
from lark_oapi.api.im.v1 import ListMessageRequest
from typing import Optional, List
from pathlib import Path

# 离线状态文件
OFFLINE_STATE_FILE = Path(__file__).parent.parent / "zhiwei-dev" / "offline_state.json"


class OfflineRecovery:
    """离线消息恢复管理器"""

    def __init__(self, client: lark.Client, bot_id: str):
        self.client = client
        self.bot_id = bot_id
        self._load_state()

    def _load_state(self):
        """加载离线状态"""
        self.state = {
            "last_disconnect_time": None,
            "last_reconnect_time": None,
            "chat_id_cache": {}  # user_id -> chat_id 映射
        }
        try:
            if OFFLINE_STATE_FILE.exists():
                with open(OFFLINE_STATE_FILE) as f:
                    loaded = json.load(f)
                    self.state.update(loaded)
        except (json.JSONDecodeError, IOError):
            pass  # 离线状态加载失败，使用默认状态

    def _save_state(self):
        """保存离线状态"""
        try:
            OFFLINE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(OFFLINE_STATE_FILE, "w") as f:
                json.dump(self.state, f, indent=2)
        except Exception as e:
            print(f"⚠️ 保存离线状态失败: {e}")

    def cache_chat_id(self, user_id: str, chat_id: str, alt_user_id: str = None):
        """
        缓存用户 chat_id（同时用 open_id 和 user_id 做 key）
        """
        if "chat_id_cache" not in self.state:
            self.state["chat_id_cache"] = {}
        self.state["chat_id_cache"][user_id] = chat_id
        if alt_user_id and alt_user_id != user_id:
            self.state["chat_id_cache"][alt_user_id] = chat_id
        self._save_state()

    def get_cached_chat_id(self, user_id: str) -> Optional[str]:
        """
        获取缓存的 chat_id

        Args:
            user_id: 用户 ID (open_id)

        Returns:
            chat_id 或 None
        """
        cache = self.state.get("chat_id_cache", {})
        return cache.get(user_id)

    def record_disconnect(self):
        """记录断连时间"""
        self.state["last_disconnect_time"] = time.time()
        self._save_state()
        print(f"📝 记录断连时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.state['last_disconnect_time']))}")

    def record_reconnect(self):
        """记录重连时间"""
        self.state["last_reconnect_time"] = time.time()
        self._save_state()

    def get_offline_duration(self) -> Optional[float]:
        """获取离线时长（秒）"""
        if not self.state.get("last_disconnect_time"):
            return None

        if not self.state.get("last_reconnect_time"):
            return None

        duration = self.state["last_reconnect_time"] - self.state["last_disconnect_time"]
        return duration if duration > 0 else None

    def get_p2p_chat_id(self, user_id: str, id_type: str = "open_id") -> Optional[str]:
        """
        获取与用户的私聊会话 ID（从缓存中获取）

        Args:
            user_id: 用户 ID
            id_type: ID 类型 (open_id, user_id, union_id) - 保留参数兼容性

        Returns:
            私聊会话 ID 或 None
        """
        # 优先从缓存获取
        cached = self.get_cached_chat_id(user_id)
        if cached:
            return cached

        print(f"⚠️ 未找到用户 {user_id[:8]}... 的缓存 chat_id，无法恢复离线消息")
        print(f"   提示：chat_id 会在收到用户消息时自动缓存")
        return None

    def recover_messages(self, chat_id: str, since_time: float) -> List[dict]:
        """
        恢复指定时间后的消息

        Args:
            chat_id: 会话 ID
            since_time: 起始时间（Unix 时间戳）

        Returns:
            消息列表
        """
        try:
            import time

            # 飞书 API 返回消息是时间正序（旧的在前）
            # 使用 end_time 获取最新消息，然后遍历到末尾
            now_ms = int(time.time() * 1000)
            since_ms = since_time * 1000

            all_messages = []
            page_token = None

            # 遍历所有页面获取完整消息列表
            for _ in range(20):  # 最多 20 页
                builder = ListMessageRequest.builder() \
                    .container_id_type("chat") \
                    .container_id(chat_id) \
                    .page_size(50)

                if page_token:
                    builder = builder.page_token(page_token)

                request = builder.build()
                response = self.client.im.v1.message.list(request)

                if not response.success():
                    print(f"❌ 获取离线消息失败: code={response.code}, msg={response.msg}")
                    break

                messages = response.data.items or []
                all_messages.extend(messages)

                # 检查是否有更多
                has_more = getattr(response.data, 'has_more', False)
                if not has_more:
                    break

                page_token = response.data.page_token
                if not page_token:
                    break

            if all_messages:
                print(f"📬 获取到 {len(all_messages)} 条历史消息")

            # 过滤：1) 机器人发送的消息 2) 时间早于 since_time 的消息
            user_messages = []
            for msg in all_messages:
                # 跳过机器人发送的消息
                if not msg.sender or msg.sender.id == self.bot_id:
                    continue
                # 按时间过滤（create_time 是毫秒时间戳，可能是字符串或整数）
                msg_time = getattr(msg, 'create_time', 0)
                if msg_time:
                    try:
                        msg_time = int(msg_time) if isinstance(msg_time, str) else msg_time
                        if msg_time >= since_ms:
                            user_messages.append(msg)
                    except (ValueError, TypeError):
                        user_messages.append(msg)  # 时间解析失败时保留消息

            return user_messages

        except Exception as e:
            print(f"❌ 离线恢复异常: {e}")
            return []

    def should_recover(self, threshold_seconds: int = 300) -> bool:
        """
        判断是否需要执行离线恢复

        Args:
            threshold_seconds: 离线阈值（秒），默认 5 分钟

        Returns:
            是否需要恢复
        """
        duration = self.get_offline_duration()
        if duration is None:
            return False

        return duration > threshold_seconds


# 全局实例（延迟初始化）
_offline_recovery: Optional[OfflineRecovery] = None


def init_offline_recovery(client: lark.Client, bot_id: str):
    """初始化离线恢复模块"""
    global _offline_recovery
    _offline_recovery = OfflineRecovery(client, bot_id)
    print("✅ 离线恢复模块已初始化")


def get_offline_recovery() -> Optional[OfflineRecovery]:
    """获取离线恢复实例"""
    return _offline_recovery