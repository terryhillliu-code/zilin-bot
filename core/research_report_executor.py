"""研究报告执行器 - 处理 [ACTION: RESEARCH_REPORT] 指令

v3.0: Obsidian 优先 + 快速模式默认 + 自动模板选择
"""
import os
import sys
import json
import subprocess
import logging
import re
import yaml
from pathlib import Path
from .persona_service import persona_service

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("research_executor")

# 硬件相关关键词 (用于自动选择 hardware_report 模板)
HARDWARE_KEYWORDS = [
    "处理器", "CPU", "GPU", "海光", "鲲鹏", "飞腾", "Intel", "AMD",
    "芯片", "Die", "NUMA", "DDR", "PCIe", "NVMe", "CXL", "服务器",
    "内存", "架构", "选型", "TCO", "散热", "液冷", "加速器", "DPU",
    "Hygon", "Ampere", "Graviton", "存储", "网卡", "RDMA", "InfiniBand",
    "chiplet", "互联", "HBM", "SPEC", "功耗", "TDP"
]


class ResearchReportExecutor:
    def __init__(self):
        self.bot_dir = Path(__file__).parent.parent
        self.analyzer_dir = Path("/Users/liufang/arxiv-paper-analyzer/backend")
        self.analyzer_python = self.analyzer_dir / "venv" / "bin" / "python3"
        self.export_root = Path("/tmp/notebooklm_export")

    def _detect_template(self, topic: str) -> str:
        """基于主题自动选择最合适的模板"""
        topic_lower = topic.lower()
        for kw in HARDWARE_KEYWORDS:
            if kw.lower() in topic_lower:
                logger.info(f"🔧 自动选择 hardware_report 模板 (命中关键词: {kw})")
                return "hardware_report"
        return "default"

    def execute(self, topic: str, user_id: str, message_id: str, reply_func, reply_card_func):
        """执行研究报告生成流程"""
        try:
            # 1. 整理文件 (清理上一次的导出，防止混淆)
            if self.export_root.exists():
                logger.info(f"清理导出分区... {self.export_root}")

            # 2. 解析参数
            template_key = None  # None = 自动检测
            actual_topic = topic
            include_videos = False
            video_limit = 5
            deep_mode = False

            remaining = topic
            if " --template=" in remaining:
                remaining, template_key = remaining.split(" --template=", 1)
                template_key = template_key.split(" --")[0].strip()
            if " --include-videos" in remaining:
                include_videos = True
                remaining = remaining.replace(" --include-videos", "")
            if " --video-limit=" in topic:
                match = re.search(r"--video-limit=(\d+)", topic)
                if match:
                    video_limit = int(match.group(1))
                    remaining = re.sub(r"--video-limit=\d+", "", remaining)
            if " --deep" in remaining:
                deep_mode = True
                remaining = remaining.replace(" --deep", "")
            if " --" in remaining:
                remaining = remaining.split(" --", 1)[0].strip()

            actual_topic = remaining.strip()

            # 3. 自动选择模板 (如果用户未显式指定)
            if template_key is None:
                template_key = self._detect_template(actual_topic)

            # 4. 构建 manage.py 命令
            cmd = [
                str(self.analyzer_python), "scripts/manage.py", "export-notebook",
                "--query", actual_topic,
                "--limit", "10",
                "--tiers", "A,B",
                "--template", template_key,
                "--obsidian-limit", "10",
            ]

            # v3.0: 仅在 deep 模式下启用 ArXiv 搜索
            if deep_mode:
                cmd.append("--auto-search")

            # 5. 获取并注入个人画像
            persona_text = persona_service.get_persona()
            if persona_text:
                safe_persona = persona_text[:1500].replace('"', "'")
                cmd.extend(["--persona", safe_persona])

            # 视频笔记
            if include_videos:
                cmd.extend(["--include-videos", "--video-limit", str(video_limit)])

            logger.info(f"执行指令 (deep={deep_mode}): {' '.join(cmd[:8])}...")

            # v3.0: 快速模式 timeout=60s, 深度模式 timeout=300s
            timeout = 300 if deep_mode else 60
            result = subprocess.run(cmd, cwd=str(self.analyzer_dir),
                                    capture_output=True, text=True, timeout=timeout)

            if result.returncode != 0:
                reply_func(message_id, f"❌ 研究包准备失败:\n{result.stderr[:200]}")
                return

            # 6. 解析结果
            combined_output = result.stdout + result.stderr

            total_match = re.search(r"总计: (\d+) 个素材", combined_output)
            obsidian_match = re.search(r"- Obsidian 笔记: (\d+)", combined_output)
            paper_match = re.search(r"- 论文: (\d+)", combined_output)
            video_match = re.search(r"- 视频笔记: (\d+)", combined_output)

            if total_match:
                total_count = int(total_match.group(1))
                obsidian_count = int(obsidian_match.group(1)) if obsidian_match else 0
                paper_count = int(paper_match.group(1)) if paper_match else 0
                video_count = int(video_match.group(1)) if video_match else 0
            else:
                success_match = re.search(r"成功: (\d+) / (\d+)", combined_output)
                total_count = int(success_match.group(1)) if success_match else 0
                obsidian_count = 0
                paper_count = total_count
                video_count = 0

            if total_count == 0:
                reply_func(message_id,
                    f"🔍 未找到关于「{actual_topic}」的素材。\n"
                    f"💡 提示: 使用 `/research {actual_topic} --deep` 可从 ArXiv 搜索并深度分析。")
                return

            # 7. 发送结果卡片
            super_prompt = self._get_template_prompt(template_key, actual_topic)

            card_content = f"### 📊 「{actual_topic}」研究素材已就绪\n\n"
            card_content += f"精准筛选了 **{total_count}** 个核心素材：\n\n"
            if obsidian_count > 0:
                card_content += f"- 📓 Obsidian 笔记：{obsidian_count} 篇\n"
            card_content += f"- 📄 论文：{paper_count} 篇\n"
            if video_count > 0:
                card_content += f"- 📹 视频笔记：{video_count} 篇\n"
            card_content += f"\n📂 **本地路径**：`{self.export_root}`\n"
            card_content += f"🎨 **使用模板**：`{template_key}`\n\n"

            if not deep_mode:
                card_content += f"💡 需要更多学术论文？发送 `/research {actual_topic} --deep`\n\n"

            card_content += "---\n"
            card_content += "#### 💡 NotebookLM 进阶指令\n"
            card_content += f"```markdown\n{super_prompt}\n```"

            reply_card_func(message_id, f"📑 {actual_topic} 研究情报就绪", card_content)

        except subprocess.TimeoutExpired:
            reply_func(message_id,
                f"⏱ 研究任务超时。快速模式已优先导出 Obsidian 笔记。\n"
                f"请检查 `{self.export_root}` 中的已完成部分。")
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