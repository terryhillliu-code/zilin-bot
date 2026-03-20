"""
离线消息恢复模块
- 需要权限: im:message.p2p_msg ✅ 已开通
- WebSocket 重连后拉取离线消息
- Phase 3 of 飞书消息离线恢复方案

Created: 2026-03-18
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
        self.state = {"last_disconnect_time": None, "last_reconnect_time": None}
        try:
            if OFFLINE_STATE_FILE.exists():
                with open(OFFLINE_STATE_FILE) as f:
                    self.state = json.load(f)
        except Exception:
            pass

    def _save_state(self):
        """保存离线状态"""
        try:
            OFFLINE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(OFFLINE_STATE_FILE, "w") as f:
                json.dump(self.state, f)
        except Exception as e:
            print(f"⚠️ 保存离线状态失败: {e}")

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
        获取与用户的私聊会话 ID

        Args:
            user_id: 用户 ID
            id_type: ID 类型 (open_id, user_id, union_id)

        Returns:
            私聊会话 ID 或 None
        """
        try:
            request = GetConversationRequest.builder() \
                .build()

            # 使用 batch_get_conversation 获取或创建私聊会话
            # 简化方案：直接使用 user_id 作为 chat_id（私聊场景）
            # 飞书私聊的 chat_id 格式通常是 oc_ 开头

            # 更可靠的方案：通过 create_conversation API 获取
            from lark_oapi.api.im.v1 import CreateConversationRequest, CreateConversationRequestBody

            request = CreateConversationRequest.builder() \
                .request_body(CreateConversationRequestBody.builder()
                    .user_id_list([user_id])
                    .build()) \
                .build()

            response = self.client.im.v1.conversation.create(request)
            if response.success() and response.data:
                return response.data.conversation_id

            print(f"⚠️ 获取私聊会话失败: {response.msg}")
            return None

        except Exception as e:
            print(f"⚠️ 获取私聊会话异常: {e}")
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
            # 转换为毫秒时间戳
            start_time = str(int(since_time * 1000))

            request = ListMessageRequest.builder() \
                .container_id_type("chat") \
                .container_id(chat_id) \
                .start_time(start_time) \
                .page_size(50) \
                .build()

            response = self.client.im.v1.message.list(request)

            if not response.success():
                print(f"❌ 获取离线消息失败: code={response.code}, msg={response.msg}")
                return []

            messages = response.data.items or []
            if messages:
                print(f"📬 获取到 {len(messages)} 条历史消息")

            # 过滤机器人发送的消息，只返回用户消息
            user_messages = [
                msg for msg in messages
                if msg.sender and msg.sender.id != self.bot_id
            ]

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