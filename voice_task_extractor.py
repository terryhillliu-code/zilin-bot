#!/usr/bin/env python3
"""
语音任务提取模块
使用 LLM 从语音转文字中提取待办任务
"""

import os
import sys
import json
import logging
from pathlib import Path
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

# Prompt 文件路径
PROMPT_PATH = Path(__file__).parent / "prompts" / "voice_task_prompt.md"


def load_prompt() -> str:
    """加载任务提取 Prompt"""
    if PROMPT_PATH.exists():
        return PROMPT_PATH.read_text(encoding='utf-8')
    else:
        # 默认 Prompt
        return """你是一个任务提取助手。从用户的语音转文字中提取待办任务。

规则：
1. 识别明确的待办事项（需要做的、要完成的、要处理的）
2. 忽略纯陈述句（"今天天气不错"、"我吃了个苹果"）
3. 识别优先级：
   - high: 紧急、重要、马上要做的
   - normal: 一般任务
   - low: 可以后续处理、不急的
4. 返回 JSON 格式

输出格式：
```json
{
  "tasks": [
    {"content": "任务内容", "priority": "high/normal/low"}
  ],
  "has_no_task": false
}
```

如果没有识别到待办任务，返回：
```json
{
  "tasks": [],
  "has_no_task": true
}
```"""


def extract_tasks(text: str) -> List[Dict]:
    """从文本中提取待办任务

    Args:
        text: 语音转文字内容

    Returns:
        任务列表 [{"content": "...", "priority": "high/normal/low"}, ...]
    """
    try:
        # 导入 llm_client
        try:
            from llm_client import llm_client
        except ImportError:
            bot_dir = Path(__file__).parent
            if str(bot_dir) not in sys.path:
                sys.path.insert(0, str(bot_dir))
            from llm_client import llm_client

        # 加载 Prompt
        system_prompt = load_prompt()

        # 构建消息
        user_message = f"请从以下内容中提取待办任务：\n\n{text}"

        # 调用 LLM
        success, response = llm_client.call(
            role="format",  # 使用通微角色，适合结构化输出
            message=user_message,
            system_prompt=system_prompt,
            timeout=30
        )

        if not success or not response:
            logger.error(f"LLM 调用失败: {response}")
            return []

        # 解析 JSON
        tasks = parse_task_json(response)

        logger.info(f"✅ 从语音中提取 {len(tasks)} 个任务")
        return tasks

    except Exception as e:
        logger.error(f"任务提取异常: {e}")
        return []


def parse_task_json(response: str) -> List[Dict]:
    """从 LLM 响应中解析任务 JSON

    Args:
        response: LLM 响应文本

    Returns:
        任务列表
    """
    try:
        # 尝试提取 JSON 块
        json_str = response

        # 如果包含 markdown 代码块，提取其中的 JSON
        if "```json" in response:
            start = response.find("```json") + 7
            end = response.find("```", start)
            if end > start:
                json_str = response[start:end].strip()
        elif "```" in response:
            start = response.find("```") + 3
            end = response.find("```", start)
            if end > start:
                json_str = response[start:end].strip()

        # 解析 JSON
        data = json.loads(json_str)

        # 验证格式
        if data.get("has_no_task"):
            return []

        tasks = data.get("tasks", [])

        # 验证每个任务
        valid_tasks = []
        for task in tasks:
            if isinstance(task, dict) and task.get("content"):
                # 默认优先级
                priority = task.get("priority", "normal")
                if priority not in ["high", "normal", "low"]:
                    priority = "normal"

                valid_tasks.append({
                    "content": task["content"],
                    "priority": priority
                })

        return valid_tasks

    except json.JSONDecodeError as e:
        logger.error(f"JSON 解析失败: {e}\n原始响应: {response[:200]}")
        return []
    except Exception as e:
        logger.error(f"任务解析异常: {e}")
        return []


def extract_tasks_with_retry(text: str, max_retries: int = 2) -> List[Dict]:
    """带重试的任务提取

    Args:
        text: 语音转文字内容
        max_retries: 最大重试次数

    Returns:
        任务列表
    """
    for attempt in range(max_retries + 1):
        tasks = extract_tasks(text)
        if tasks is not None:
            return tasks

        if attempt < max_retries:
            logger.warning(f"任务提取失败，重试 {attempt + 1}/{max_retries}")

    return []


if __name__ == "__main__":
    # 测试
    test_text = "今天要完成项目报告，还有回复客户邮件，下午三点开会讨论新需求"

    print(f"📝 测试文本: {test_text}")
    tasks = extract_tasks(test_text)

    print(f"\n📋 提取结果 ({len(tasks)} 个任务):")
    for i, task in enumerate(tasks, 1):
        print(f"  {i}. [{task['priority']}] {task['content']}")