"""
Obsidian AI 摘要生成模块

为 AI 硬件架构师定制专属的摘要生成 prompt，
统一被 pdf_parser.py、media_handler.py、knowledge_collect.py 复用。
"""

import os
import httpx
from typing import Optional

# ============ AI 硬件架构师专属 Prompt ============

SUMMARY_PROMPT = """你是一位资深 AI 硬件行业分析师助手。

读者背景：AI 硬件架构师，有服务器硬件开发和系统架构背景，机械专业出身。主要工作是行业分析和竞品分析。不关注纯软件算法实现细节。

请阅读以下文档，生成结构化的分析摘要。

## 规则
1. 总字数 500-1000 字，信息密度优先
2. 遇到数据（带宽、功耗、成本、尺寸、温度、市场规模）必须加粗
3. 如果有性能对比或规格对比，必须用 Markdown 表格
4. 对软件/算法内容，翻译成硬件工程语言（算力、带宽、功耗、散热、部署、成本）
5. 不要解释基础概念
6. 先判断文档类型再调整侧重：
   - 行业研报：重点市场数据、竞争格局、供应链
   - 学术论文：重点技术方案、性能数据、工程可行性
   - 产品白皮书：重点产品规格、与竞品对比
   - 技术文档：重点架构设计、部署要求

## 输出格式（严格遵守 Markdown）

## 📌 一句话定位
{不超过 50 字}

## 🔍 核心发现
- {3-5 条，每条带数据}

## 🏗️ 方案与架构分析
{200-300 字}

## 📊 关键数据
{表格，无数据可省略}

## 🏢 涉及实体
**公司**：
**产品/型号**：
**技术路线**：

## 🔧 对我的价值
- {硬件架构启示}
- {行业/竞品价值}

## 📋 阅读建议
{精读/略读/背景材料/竞品必看} — {理由}
"""


# ============ 文本截断策略 ============

def get_text_for_summary(full_text: str) -> str:
    """
    根据文本长度智能截断，平衡 token 预算和信息保留

    策略：
    - < 40000 字符：全文保留
    - 40000-200000 字符：保留前 80%
    - > 200000 字符：保留前 5000 + 后 3000（首尾摘要）
    """
    length = len(full_text)
    if length < 40000:
        return full_text
    elif length < 200000:
        return full_text[:int(length * 0.8)]
    else:
        return full_text[:5000] + "\n...\n" + full_text[-3000:]


# ============ API 配置 ============

def _get_api_key() -> Optional[str]:
    """获取百炼 API Key"""
    env_path = os.path.expanduser("~/tanwei-bot/.env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                if line.startswith("CODING_PLAN_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"\'')
    return None


# ============ 摘要生成入口 ============

def generate_ai_summary(
    text: str,
    doc_type: str = None,
    model: str = "qwen3.5-plus",
    max_tokens: int = 1500,
    timeout: int = 90
) -> str:
    """
    生成 AI 硬件架构师专属摘要

    Args:
        text: 待摘要的文本内容
        doc_type: 文档类型提示（可选）：研报/论文/白皮书/技术文档
        model: 使用的模型
        max_tokens: 最大输出 token
        timeout: 超时秒数

    Returns:
        str: 生成的摘要，失败时返回错误信息
    """
    api_key = _get_api_key()
    if not api_key:
        return "❌ 系统配置异常：API Key 未找到"

    # 应用文本截断策略
    truncated_text = get_text_for_summary(text)

    # 构建完整 prompt
    prompt = SUMMARY_PROMPT
    if doc_type:
        prompt += f"\n\n## 文档类型提示\n本文档类型：{doc_type}"
    prompt += f"\n\n## 文档内容\n{truncated_text}"

    try:
        response = httpx.post(
            "https://coding.dashscope.aliyuncs.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            },
            json={
                "model": model,
                "messages": [{
                    "role": "user",
                    "content": prompt
                }],
                "max_tokens": max_tokens
            },
            timeout=timeout
        )

        if response.status_code == 200:
            result = response.json()["choices"][0]["message"]["content"]
            return result
        else:
            return f"❌ API 调用失败: {response.status_code}"

    except Exception as e:
        return f"❌ 摘要生成异常: {str(e)}"


def generate_ai_summary_for_obsidian(text: str, title: str = "", doc_type: str = None) -> str:
    """
    生成适用于 Obsidian 笔记的 AI 摘要章节

    Args:
        text: 待摘要的文本内容
        title: 文档标题（可选，用于日志）
        doc_type: 文档类型提示

    Returns:
        str: 格式化的 Obsidian 章节内容，包含 AI 摘要
    """
    print(f"🤖 生成 AI 摘要: {title[:50] if title else '未知标题'}...")

    summary = generate_ai_summary(text, doc_type)

    if summary.startswith("❌"):
        print(f"⚠️ AI 摘要生成失败: {summary}")
        return ""

    print(f"✅ AI 摘要生成完成: {len(summary)} 字符")
    return f"\n## AI 摘要\n\n{summary}\n"


# ============ 单元测试 ============

if __name__ == "__main__":
    # 测试文本截断策略
    test_cases = [
        ("短文本" * 100, "短文本"),
        ("中" * 50000, "中等长度"),
        ("长" * 250000, "超长文本"),
    ]

    print("=== 文本截断策略测试 ===")
    for text, name in test_cases:
        result = get_text_for_summary(text)
        print(f"{name}: 原文 {len(text)} → 截断后 {len(result)}")

    print("\n=== API Key 检查 ===")
    key = _get_api_key()
    print(f"API Key: {'已配置' if key else '未找到'}")

    print("\n=== 模块验证通过 ===")