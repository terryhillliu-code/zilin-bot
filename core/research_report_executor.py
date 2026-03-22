"""研究报告执行器 - 处理 [ACTION: RESEARCH_REPORT] 指令"""
import os
import sys
import json
import subprocess
import logging
import re
import yaml
from pathlib import Path

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("research_executor")

class ResearchReportExecutor:
    def __init__(self):
        self.bot_dir = Path(__file__).parent.parent
        self.analyzer_dir = Path("/Users/liufang/arxiv-paper-analyzer/backend")
        self.analyzer_python = self.analyzer_dir / "venv" / "bin" / "python3"
        self.export_root = Path("/tmp/notebooklm_export")

    def execute(self, topic: str, user_id: str, message_id: str, reply_func, reply_card_func):
        """执行研究报告生成流程"""
        try:
            # 1. 整理文件 (清理上一次的导出，防止混淆)
            if self.export_root.exists():
                logger.info(f"清理导出分区... {self.export_root}")

            # 2. 解析 Topic 和 Template
            # 兼容格式: "主题 --template=podcast" 或 "主题"
            template_key = "default"
            actual_topic = topic
            if " --template=" in topic:
                actual_topic, template_key = topic.split(" --template=", 1)
            elif " --" in topic:
                 actual_topic = topic.split(" --", 1)[0].strip()

            # 3. 调用 manage.py export-notebook 执行过滤和导出
            # v2.1: 默认启用 --auto-search，自动从 ArXiv 补充
            cmd = [
                str(self.analyzer_python), "scripts/manage.py", "export-notebook",
                "--query", actual_topic,
                "--limit", "10",
                "--tiers", "A,B",
                "--template", template_key,
                "--auto-search"
            ]

            logger.info(f"执行同步指令: {' '.join(cmd)}")
            result = subprocess.run(cmd, cwd=str(self.analyzer_dir), capture_output=True, text=True, timeout=120)

            if result.returncode != 0:
                reply_func(message_id, f"❌ 研究包准备失败:\n{result.stderr[:200]}")
                return

            # 3. 检查生成结果
            # 根据日志判断成功数量 (注意: logging 默认在 stderr)
            combined_output = result.stdout + result.stderr
            success_match = re.search(r"成功: (\d+) / (\d+)", combined_output)
            success_count = int(success_match.group(1)) if success_match else 0

            # 检查是否触发了 ArXiv 搜索
            arxiv_search_triggered = "ArXiv 全时域搜索" in combined_output

            if success_count == 0:
                if arxiv_search_triggered:
                    reply_func(message_id, f"🔍 已尝试从 ArXiv 搜索，但仍未找到关于「{actual_topic}」的高质量文献。请尝试更具体的关键词。")
                else:
                    reply_func(message_id, f"🔍 抱歉，在本地库中未找到关于「{actual_topic}」的高质量已分析文献 (Tier A/B)。建议您先收录相关 ArXiv 论文。")
                return

            # 4. 获取对应的超级提示词显示
            super_prompt = self._get_template_prompt(template_key, actual_topic)

            # 5. 发送精美的结果卡片
            card_content = f"### 📊 「{actual_topic}」研究素材已就绪\n\n"
            if arxiv_search_triggered:
                card_content += "🌐 已自动从 ArXiv 搜索并补充文献。\n\n"
            card_content += f"我为您精准筛选并清洗了 **{success_count}** 篇核心文献报告。\n\n"
            card_content += f"📂 **本地暂存路径**：\n`{self.export_root}`\n\n"
            card_content += "---\n"
            card_content += "#### 💡 NotebookLM 进阶指令 (建议复制使用)\n"
            card_content += "请将上述文件夹内的文件导入 NotebookLM 后，使用以下提示词开启深度研究：\n\n"
            card_content += f"```markdown\n{super_prompt}\n```"

            reply_card_func(message_id, f"📑 {actual_topic} 研究情报就绪", card_content)

        except Exception as e:
            logger.error(f"执行研究任务异常: {e}")
            reply_func(message_id, f"❌ 研究流程中断：{str(e)}")

    def _get_template_prompt(self, template_key: str, topic: str) -> str:
        """从 YAML 获取模板内容"""
        template_path = self.analyzer_dir / "app" / "notebooklm_templates.yaml"
        try:
            if template_path.exists():
                with open(template_path, 'r', encoding='utf-8') as f:
                    data = yaml.safe_load(f)
                    prompt = data.get(template_key, {}).get("prompt", "")
                    if not prompt:
                         prompt = data.get("default", {}).get("prompt", "")
                    return prompt.replace("{topic}", topic)
        except Exception as e:
            logger.error(f"加载模板失败: {e}")

        return f"请基于提供的关于 {topic} 的文献进行深度研究。"

# 单例
research_executor = ResearchReportExecutor()