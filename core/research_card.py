"""飞书交互卡片封装

提供研究配置卡片等交互式卡片的构建和发送功能。

v47.0 飞书交互智能化 Phase 2
"""

import json
import logging
from typing import Dict, Any, Optional, List

logger = logging.getLogger(__name__)


class ResearchConfigCard:
    """研究配置卡片构建器"""

    @staticmethod
    def build(
        topic: str = "",
        include_videos: bool = True,
        templates: List[str] = None
    ) -> Dict[str, Any]:
        """
        构建研究配置卡片

        Args:
            topic: 研究主题
            include_videos: 是否包含视频笔记
            templates: 可用模板列表

        Returns:
            飞书卡片 JSON 结构
        """
        if templates is None:
            templates = ["default", "tech_comparison", "podcast_script"]

        template_options = []
        template_names = {
            "default": "📝 默认模板",
            "tech_comparison": "⚖️ 技术对比",
            "podcast_script": "🎙️ 播客脚本"
        }
        for t in templates:
            template_options.append({
                "text": {"tag": "plain_text", "content": template_names.get(t, t)},
                "value": t
            })

        card = {
            "type": "template",
            "data": {
                "template_id": "AAqkHM28AA",  # 可选：使用飞书模板
                "template_variable": {
                    "title": "📊 研究配置",
                    "topic_label": "研究主题",
                    "topic_value": topic,
                    "template_label": "输出模板",
                    "template_options": template_options,
                    "include_videos": include_videos
                }
            }
        }

        # 如果没有模板 ID，使用原生卡片
        card = {
            "config": {
                "wide_screen_mode": True
            },
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": "📊 NotebookLM 研究配置"
                },
                "template": "blue"
            },
            "elements": [
                # 主题输入
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**研究主题**\n{topic if topic else '请确认或修改主题'}"
                    }
                },
                {
                    "tag": "hr"
                },
                # 配置选项
                {
                    "tag": "div",
                    "fields": [
                        {
                            "is_short": True,
                            "text": {
                                "tag": "lark_md",
                                "content": f"**来源**: 论文库 + ArXiv"
                            }
                        },
                        {
                            "is_short": True,
                            "text": {
                                "tag": "lark_md",
                                "content": f"**类型**: 论文{' + 视频笔记' if include_videos else ''}"
                            }
                        }
                    ]
                },
                {
                    "tag": "div",
                    "fields": [
                        {
                            "is_short": True,
                            "text": {
                                "tag": "lark_md",
                                "content": f"**数量**: 10 篇"
                            }
                        },
                        {
                            "is_short": True,
                            "text": {
                                "tag": "lark_md",
                                "content": f"**模板**: 默认模板"
                            }
                        }
                    ]
                },
                {
                    "tag": "hr"
                },
                # 操作按钮
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {
                                "tag": "plain_text",
                                "content": "🚀 开始研究"
                            },
                            "type": "primary",
                            "value": {
                                "action": "start_research",
                                "topic": topic,
                                "include_videos": str(include_videos).lower()
                            }
                        },
                        {
                            "tag": "button",
                            "text": {
                                "tag": "plain_text",
                                "content": "⚙️ 修改配置"
                            },
                            "type": "default",
                            "value": {
                                "action": "show_config_form",
                                "topic": topic
                            }
                        },
                        {
                            "tag": "button",
                            "text": {
                                "tag": "plain_text",
                                "content": "❌ 取消"
                            },
                            "type": "default",
                            "value": {
                                "action": "cancel_research"
                            }
                        }
                    ]
                },
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": "💡 提示：点击「开始研究」后将自动检索、清洗并打包素材"
                        }
                    ]
                }
            ]
        }

        return card

    @staticmethod
    def build_simple_confirm(topic: str, include_videos: bool = True, reasoning: str = "", confidence: float = 0.95) -> Dict[str, Any]:
        """
        构建简单确认卡片（无输入框版本）

        用于意图识别后的快速确认
        """
        return {
            "config": {
                "wide_screen_mode": True
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"### 🤔 检测到研究意图\n\n您想让我整理一份关于「**{topic}**」的研究报告吗？"
                    }
                },
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"🎯 **对齐置信度**: {int(confidence * 100)}% | {'✅ 包含视频' if include_videos else '📄 仅论文'}"
                    }
                },
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"🧠 **AI 决策理由**:\n{reasoning if reasoning else '基于您的历史对话，为您提供深度研究支持。'}"
                    }
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "✅ 是的，开始研究"},
                            "type": "primary",
                            "value": {
                                "action": "start_research",
                                "topic": topic,
                                "include_videos": str(include_videos).lower()
                            }
                        },
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "💬 只是聊聊"},
                            "type": "default",
                            "value": {"action": "cancel_research"}
                        }
                    ]
                }
            ]
        }


class ResearchResultCard:
    """研究结果卡片构建器"""

    @staticmethod
    def build(
        topic: str,
        paper_count: int,
        video_count: int = 0,
        arxiv_triggered: bool = False,
        export_path: str = "/tmp/notebooklm_export"
    ) -> Dict[str, Any]:
        """
        构建研究结果卡片
        """
        total = paper_count + video_count

        content = f"### 📊 「{topic}」研究素材已就绪\n\n"

        if arxiv_triggered:
            content += "🌐 已自动从 ArXiv 搜索并补充文献。\n\n"

        content += f"我为您精准筛选并清洗了 **{total}** 个核心素材：\n\n"
        content += f"- 📄 论文：{paper_count} 篇\n"
        if video_count > 0:
            content += f"- 📹 视频笔记：{video_count} 篇\n"

        return {
            "config": {"wide_screen_mode": True},
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": content
                    }
                },
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"📂 **本地暂存路径**：\n`{export_path}`"
                    }
                },
                {
                    "tag": "hr"
                },
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": "#### 💡 NotebookLM 进阶指令\n请将上述文件夹内的文件导入 NotebookLM 后，使用提示词开启深度研究。"
                    }
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "📁 打开文件夹"},
                            "type": "default",
                            "value": {"action": "open_folder", "path": export_path}
                        }
                    ]
                }
            ]
        }


# 便捷函数
def send_research_config_card(reply_card_func, message_id: str, topic: str, include_videos: bool = True, reasoning: str = "", confidence: float = 0.95):
    """发送研究配置卡片 (v8.0: 包含置信度与理由)"""
    card = ResearchConfigCard.build_simple_confirm(topic, include_videos, reasoning, confidence)
    reply_card_func(message_id, "📊 研究配置确认", json.dumps(card, ensure_ascii=False))


def send_research_result_card(reply_card_func, message_id: str, topic: str,
                               paper_count: int, video_count: int = 0,
                               arxiv_triggered: bool = False):
    """发送研究结果卡片"""
    card = ResearchResultCard.build(topic, paper_count, video_count, arxiv_triggered)
    reply_card_func(message_id, f"📑 {topic} 研究情报就绪", json.dumps(card, ensure_ascii=False))


# 测试
if __name__ == "__main__":
    card = ResearchConfigCard.build_simple_confirm("AI Agent", include_videos=True)
    print(json.dumps(card, ensure_ascii=False, indent=2))