"""通用确认卡片构建器（2026-07-26 P1 自然语言路由）

卡片结构沿用 core/research_card.py；按钮 value dict 在 card.action.trigger
回调中原样返回（分发器见 ws_client.py do_p2_card_action_trigger_v1）。
"""
from typing import Any, Dict


def build_confirmation(title: str, summary: str, confirm_action: str,
                       confirm_value: Dict[str, Any], confirm_label: str = "✅ 确认",
                       cancel_label: str = "取消",
                       cancel_action: str = "cancel_nl_action") -> Dict[str, Any]:
    """通用双按钮确认卡片"""
    return {
        "config": {"wide_screen_mode": True},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": f"### {title}\n\n{summary}"}},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": confirm_label},
                        "type": "primary",
                        "value": {"action": confirm_action, **confirm_value},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": cancel_label},
                        "type": "default",
                        "value": {"action": cancel_action},
                    },
                ],
            },
            {"tag": "note", "elements": [
                {"tag": "plain_text", "content": "也可以直接回复「确认」或「取消」"}
            ]},
        ],
    }


def build_capture_receipt(filename: str, filepath: str) -> Dict[str, Any]:
    """捕获回执卡片（带撤销按钮）"""
    return {
        "config": {"wide_screen_mode": True},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md",
                                    "content": f"✅ **已捕获**\n\n📄 `{filename}` · 等待 Ingest 处理"}},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "↩️ 撤销"},
                        "type": "default",
                        "value": {"action": "undo_capture", "filepath": filepath},
                    },
                ],
            },
        ],
    }
