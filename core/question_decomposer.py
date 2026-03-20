"""
问题分解器 - 将复杂问题拆分为子问题

借鉴自 Kotaemon 的多跳问答能力，增强复杂研究问题的处理。

特性：
- 自动判断是否需要分解
- 分解复杂问题为独立子问题
- 综合子问题答案生成最终回答

使用：
    from core.question_decomposer import QuestionDecomposer

    decomposer = QuestionDecomposer(llm_client)

    if decomposer.should_decompose(question):
        sub_questions = decomposer.decompose(question)
        answers = [answer_single(sq) for sq in sub_questions]
        final = decomposer.synthesize(question, answers)
"""
import json
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)


class QuestionDecomposer:
    """
    问题分解器

    用于处理复杂问题，将其拆分为可独立回答的子问题。
    """

    # 分解指标词（复杂问题的特征）
    DECOMPOSE_INDICATORS = [
        # 连接词
        "和", "以及", "同时", "并且", "还有",
        # 对比词
        "对比", "比较", "区别", "差异", "各自",
        # 序列词
        "首先", "然后", "接着", "最后",
    ]

    # 不应该分解的问题类型（简单问题特征）
    SIMPLE_PATTERNS = [
        r"^什么是",      # 定义类
        r"^怎么.{0,2}用",  # 操作类
        r"^如何.{0,2}做",  # 方法类
        r"^如何实现",     # 实现类
        r"^[几多]点",     # 时间类
        r"^在哪",        # 位置类
        r"^谁",          # 人物类
    ]

    # 强分解指标（出现即分解）
    STRONG_INDICATORS = [
        "对比", "比较", "区别", "差异",
        "首先", "然后", "以及",
    ]

    def __init__(self, llm_client=None, max_sub_questions: int = 4):
        """
        初始化问题分解器

        Args:
            llm_client: LLM 客户端（用于分解和综合）
            max_sub_questions: 最大子问题数量
        """
        self.llm = llm_client
        self.max_sub_questions = max_sub_questions

    def should_decompose(self, question: str) -> bool:
        """
        判断问题是否需要分解

        Args:
            question: 用户问题

        Returns:
            True 如果需要分解，False 如果可以直接回答
        """
        # 空问题不分解
        if not question or not question.strip():
            return False

        question = question.strip()

        # 简单问题模式匹配
        for pattern in self.SIMPLE_PATTERNS:
            if re.search(pattern, question):
                return False

        # 强指标检测（出现即分解）
        for indicator in self.STRONG_INDICATORS:
            if indicator in question:
                logger.info(f"[Decomposer] 检测到强指标 '{indicator}'，需要分解")
                return True

        # 计算普通指标
        indicator_count = sum(
            1 for indicator in self.DECOMPOSE_INDICATORS
            if indicator in question
        )

        # 统计问号数量
        question_mark_count = question.count("?") + question.count("？")

        # 判断条件：
        # 1. 指标词 >= 2 个
        # 2. 或者问号 >= 2 个
        should_split = indicator_count >= 2 or question_mark_count >= 2

        if should_split:
            logger.info(f"[Decomposer] 判断需要分解: {question[:50]}...")

        return should_split

    def decompose(self, question: str) -> list[str]:
        """
        分解复杂问题为子问题列表

        Args:
            question: 用户问题

        Returns:
            子问题列表
        """
        if not self.llm:
            logger.warning("[Decomposer] LLM 客户端未配置，返回原问题")
            return [question]

        prompt = f"""将以下复杂问题分解为 2-{self.max_sub_questions} 个独立的子问题。

原问题：{question}

要求：
1. 每个子问题独立可回答
2. 子问题之间有逻辑顺序（先回答的为后面提供背景）
3. 回答所有子问题后能综合回答原问题
4. 子问题应该是具体、明确的问题

输出格式（纯 JSON，不要 markdown 代码块）：
{{"sub_questions": ["子问题1", "子问题2", ...]}}
"""

        try:
            success, response = self.llm.call("main", prompt)
            if not success:
                logger.warning(f"[Decomposer] LLM 调用失败: {response}")
                return [question]

            # 解析 JSON
            # 尝试提取 JSON 块
            json_match = re.search(r'\{[^{}]*"sub_questions"[^{}]*\}', response, re.DOTALL)
            if json_match:
                json_str = json_match.group()
            else:
                json_str = response

            result = json.loads(json_str)
            sub_questions = result.get("sub_questions", [])

            if not sub_questions:
                logger.warning("[Decomposer] 未能提取子问题")
                return [question]

            # 限制数量
            sub_questions = sub_questions[:self.max_sub_questions]

            logger.info(f"[Decomposer] 分解为 {len(sub_questions)} 个子问题")
            for i, sq in enumerate(sub_questions, 1):
                logger.info(f"  {i}. {sq}")

            return sub_questions

        except json.JSONDecodeError as e:
            logger.warning(f"[Decomposer] JSON 解析失败: {e}")
            return [question]
        except Exception as e:
            logger.error(f"[Decomposer] 分解异常: {e}")
            return [question]

    def synthesize(
        self,
        original_question: str,
        sub_answers: list[str]
    ) -> str:
        """
        综合子问题答案，生成最终回答

        Args:
            original_question: 原始问题
            sub_answers: 子问题答案列表

        Returns:
            综合后的回答
        """
        if not self.llm:
            # 无 LLM，简单拼接
            return "\n\n".join(sub_answers)

        if len(sub_answers) == 1:
            return sub_answers[0]

        # 构建综合 prompt
        answers_text = "\n\n---\n\n".join(
            f"### 部分 {i+1}\n{ans}"
            for i, ans in enumerate(sub_answers)
        )

        prompt = f"""基于以下各部分的回答，综合生成对原问题的完整回答。

原问题：{original_question}

各部分回答：
{answers_text}

要求：
1. 整合各部分信息，不要简单拼接
2. 保留关键数据和观点
3. 组织成连贯的回答
4. 如有矛盾，说明不同观点

请直接给出综合回答："""

        try:
            success, response = self.llm.call("main", prompt)
            if success:
                return response
            else:
                logger.warning(f"[Decomposer] 综合失败: {response}")
                return "\n\n".join(sub_answers)

        except Exception as e:
            logger.error(f"[Decomposer] 综合异常: {e}")
            return "\n\n".join(sub_answers)


# 全局单例（延迟初始化）
_decomposer_instance: Optional[QuestionDecomposer] = None


def get_decomposer(llm_client=None) -> QuestionDecomposer:
    """
    获取问题分解器单例

    Args:
        llm_client: LLM 客户端（仅首次调用时需要）

    Returns:
        QuestionDecomposer 实例
    """
    global _decomposer_instance

    if _decomposer_instance is None:
        _decomposer_instance = QuestionDecomposer(llm_client)

    return _decomposer_instance


# 测试
if __name__ == "__main__":
    # 测试问题分解判断
    decomposer = QuestionDecomposer()

    test_questions = [
        "什么是 RAG？",  # 简单问题
        "对比 GPT-4 和 Claude 的能力，以及它们各自的优势是什么？",  # 复杂问题
        "如何实现向量检索？",  # 简单问题
        "为什么 LLM 会有幻觉问题？如何缓解？",  # 中等问题
        "分析 RAG 和 Fine-tuning 的区别，以及在什么场景下应该选择哪个？",  # 复杂问题
    ]

    print("=== 问题分解测试 ===\n")
    for q in test_questions:
        should = decomposer.should_decompose(q)
        print(f"问题: {q}")
        print(f"需要分解: {'是' if should else '否'}")
        print()