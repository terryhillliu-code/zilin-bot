"""NLU 意图识别器

实现自然语言到系统意图的映射，支持：
- 研究意图识别（触发 NotebookLM 导出）
- 搜索意图识别
- 实体提取（主题、来源、是否含视频）

v47.0 飞书交互智能化核心组件
"""

import re
import logging
from typing import Dict, Any, List, Tuple
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class IntentResult:
    """意图识别结果"""
    intent: str  # research, search, chat
    confidence: float  # 0.0 - 1.0
    entities: Dict[str, Any]

    def is_research_intent(self, threshold: float = 0.6) -> bool:
        """是否为研究意图"""
        return self.intent == "research" and self.confidence >= threshold


class IntentRecognizer:
    """NLU 意图识别器"""

    # 意图模式定义
    INTENT_PATTERNS = {
        "research": {
            "strong": [
                r"帮我研究(一下|下)?",
                r"研究(一下|下)?\s*.+",
                r"整理一份.*报告",
                r"整理(一下|下)?.*资料",
                r"分析(一下|下)?.*技术",
                r"生成.*研究报告",
            ],
            "medium": [
                r"看看有什么.*资料",
                r"了解(一下|下)?\s*.+",
                r"有什么.*相关",
            ],
        },
        "search": [
            r"搜(一下|下)?",
            r"找(一找|一下|下)?",
            r"查(一下|下)?",
        ]
    }

    # 实体提取模式
    ENTITY_PATTERNS = {
        "include_videos": [
            r"包括视频",
            r"含.*视频",
            r"视频.*也",
            r"加上视频",
        ],
        "exclude_videos": [
            r"只看论文",
            r"只要论文",
            r"不要视频",
            r"排除视频",
        ],
        "source_online": [
            r"从\s*(网上|全网|互联网)",
            r"在线",
            r"online",
        ],
        "source_local": [
            r"只在.*本地",
            r"本地库",
            r"不要联网",
        ],
    }

    # 置信度权重
    CONFIDENCE_WEIGHTS = {
        "strong": 0.95,
        "medium": 0.75,
        "weak": 0.4,
    }

    def __init__(self, intent_threshold: float = 0.6):
        """
        Args:
            intent_threshold: 意图置信度阈值
        """
        self.intent_threshold = intent_threshold
        self._compile_patterns()

    def _compile_patterns(self):
        """预编译正则表达式"""
        self._compiled = {}

        # 编译意图模式
        for intent, patterns in self.INTENT_PATTERNS.items():
            if isinstance(patterns, dict):
                self._compiled[intent] = {
                    level: [re.compile(p, re.IGNORECASE) for p in ps]
                    for level, ps in patterns.items()
                }
            else:
                self._compiled[intent] = {
                    "default": [re.compile(p, re.IGNORECASE) for p in patterns]
                }

        # 编译实体模式
        self._compiled["entities"] = {
            entity: [re.compile(p, re.IGNORECASE) for p in patterns]
            for entity, patterns in self.ENTITY_PATTERNS.items()
        }

    def recognize(self, text: str) -> IntentResult:
        """
        识别文本意图

        Args:
            text: 用户输入文本

        Returns:
            IntentResult 包含意图、置信度和实体
        """
        # 1. 研究意图检测
        research_result = self._detect_research_intent(text)
        if research_result:
            intent, confidence, entities = research_result
            return IntentResult(intent, confidence, entities)

        # 2. 搜索意图检测
        search_result = self._detect_search_intent(text)
        if search_result:
            intent, confidence, entities = search_result
            return IntentResult(intent, confidence, entities)

        # 3. 默认为普通对话
        return IntentResult(
            intent="chat",
            confidence=0.9,
            entities={"topic": None}
        )

    def _detect_research_intent(self, text: str) -> Tuple[str, float, Dict] | None:
        """检测研究意图"""
        patterns = self._compiled.get("research", {})

        # 检查强意图
        for pattern in patterns.get("strong", []):
            if pattern.search(text):
                topic = self._extract_topic(text, pattern)
                entities = self._extract_entities(text)
                entities["topic"] = topic
                return ("research", self.CONFIDENCE_WEIGHTS["strong"], entities)

        # 检查中意图
        for pattern in patterns.get("medium", []):
            if pattern.search(text):
                topic = self._extract_topic(text, pattern)
                entities = self._extract_entities(text)
                entities["topic"] = topic
                return ("research", self.CONFIDENCE_WEIGHTS["medium"], entities)

        return None

    def _detect_search_intent(self, text: str) -> Tuple[str, float, Dict] | None:
        """检测搜索意图"""
        patterns = self._compiled.get("search", {}).get("default", [])

        for pattern in patterns:
            if pattern.search(text):
                topic = self._extract_topic(text, pattern)
                entities = self._extract_entities(text)
                entities["topic"] = topic
                return ("search", self.CONFIDENCE_WEIGHTS["weak"], entities)

        return None

    def _extract_topic(self, text: str, matched_pattern: re.Pattern) -> str:
        """
        提取研究主题

        策略：识别并移除触发词和修饰词，保留核心主题
        """
        topic = text

        # 1. 移除触发动词短语
        trigger_patterns = [
            r"帮我研究(一下|下)?",
            r"研究(一下|下)?",
            r"整理(一份)?",
            r"分析(一下|下)?",
            r"了解(一下|下)?",
            r"看看有什么",
            r"看看",
        ]
        for pattern in trigger_patterns:
            topic = re.sub(pattern, "", topic, flags=re.IGNORECASE)

        # 2. 移除名词后缀
        suffix_patterns = [
            r"的研究报告?",
            r"研究报告?",
            r"报告",
            r"资料",
            r"相关.*",
        ]
        for pattern in suffix_patterns:
            topic = re.sub(pattern, "", topic, flags=re.IGNORECASE)

        # 3. 移除视频相关短语（已作为单独实体处理）
        video_patterns = [
            r"，包括视频.*",
            r"，含.*视频.*",
            r"加上视频.*",
            r"包括视频",
            r"含视频",
        ]
        for pattern in video_patterns:
            topic = re.sub(pattern, "", topic, flags=re.IGNORECASE)

        # 4. 清理标点和多余空格
        topic = re.sub(r"[，。、！？、]", "", topic)
        topic = re.sub(r"\s+", " ", topic).strip()

        # 5. 移除首尾的虚词
        topic = topic.strip("的下关于 ")

        return topic if topic else text

    def _extract_entities(self, text: str) -> Dict[str, Any]:
        """提取实体"""
        entities = {
            "topic": None,
            "include_videos": None,
            "source": None,
        }

        entity_patterns = self._compiled.get("entities", {})

        # 检测是否包含视频
        for pattern in entity_patterns.get("include_videos", []):
            if pattern.search(text):
                entities["include_videos"] = True
                break

        for pattern in entity_patterns.get("exclude_videos", []):
            if pattern.search(text):
                entities["include_videos"] = False
                break

        # 检测来源
        for pattern in entity_patterns.get("source_online", []):
            if pattern.search(text):
                entities["source"] = "online"
                break

        for pattern in entity_patterns.get("source_local", []):
            if pattern.search(text):
                entities["source"] = "local"
                break

        return entities


# 单例
intent_recognizer = IntentRecognizer()


# 便捷函数
def recognize_intent(text: str) -> IntentResult:
    """便捷意图识别函数"""
    return intent_recognizer.recognize(text)


# 测试
if __name__ == "__main__":
    test_cases = [
        "帮我研究下 AI Agent",
        "看看有什么关于 RAG 的资料",
        "搜一下 Transformer",
        "今天天气怎么样",
        "整理一份多模态的研究报告，包括视频",
        "分析一下 Agent 技术",
        "了解下 LangChain",
        "只看论文，不要视频",
    ]

    print("=" * 60)
    print("意图识别测试")
    print("=" * 60)

    for text in test_cases:
        result = recognize_intent(text)
        print(f"\n输入: {text}")
        print(f"  意图: {result.intent}")
        print(f"  置信度: {result.confidence:.2f}")
        print(f"  实体: {result.entities}")
        print(f"  是研究意图: {result.is_research_intent()}")