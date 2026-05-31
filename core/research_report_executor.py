"""研究报告执行器 - 处理 [ACTION: RESEARCH_REPORT] 指令

v3.0: Obsidian 优先 + 快速模式默认 + 自动模板选择
v8.0: 增加 RAG 反思层与执行前校准
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
from .llm_client import llm_client
from .claude_preprocessor import ClaudePreprocessor

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

    def _count_local_sources(self, topic: str) -> int:
        """计算本地相关数据量"""
        counts = {"obsidian": 0, "papers": 0}
        topic_lower = topic.lower()

        # 1. 计数 Obsidian 笔记
        obsidian_dir = Path("/Users/liufang/Documents/ZhiweiVault/70-79_个人笔记/播客笔记")
        if obsidian_dir.exists():
            for md_file in obsidian_dir.glob("*.md"):
                try:
                    content = md_file.read_text(encoding="utf-8")
                    if any(kw.lower() in content.lower() for kw in topic_lower.split()):
                        counts["obsidian"] += 1
                except Exception:
                    pass

        # 2. 计数论文
        try:
            paper_db = Path("/Users/liufang/arxiv-paper-analyzer/backend/data/papers.db")
            if paper_db.exists():
                import sqlite3
                conn = sqlite3.connect(str(paper_db))
                c = conn.cursor()
                for kw in topic_lower.split():
                    c.execute(
                        "SELECT COUNT(*) FROM papers WHERE LOWER(title) LIKE ? OR LOWER(abstract) LIKE ?",
                        (f"%{kw}%", f"%{kw}%")
                    )
                    count = c.fetchone()[0]
                    counts["papers"] = max(counts["papers"], count)
                conn.close()
        except Exception:
            pass

        return counts["obsidian"] + counts["papers"]

    def _route_claude_search(self, topic: str, reply_func, message_id):
        """Route C: 无本地数据 → Claude + WebSearch 直接回答"""
        reply_func(message_id, f"🔍 本地暂无关于「{topic}」的数据，正在使用 Claude + 网络搜索为您分析，请稍候...")

        prompt = f"""你是知微系统的首席技术分析师。请回答以下技术问题：

【主题】{topic}

要求：
1. 基于你的专业知识直接回答
2. 如果涉及最新趋势（2025-2026），标注【基于训练数据，可能已过时】
3. 给出具体数字和案例
4. 中文输出，1500-2500 字"""

        success, result = llm_client.call(
            role="research",
            message=prompt,
            system_prompt="你是知微系统的首席技术分析师，请直接回答用户的技术问题。",
            timeout=180
        )

        if success:
            reply_func(message_id, f"📋 **关于「{topic}」的分析报告**\n\n（注：本地暂无相关数据，以下基于 AI 内置知识）\n\n{result}")
        else:
            reply_func(message_id, f"❌ 分析失败：{result}")

    def execute(self, topic: str, user_id: str, message_id: str, reply_func, reply_card_func):
        """执行研究报告生成流程"""
        try:
            # [新增] Phase 0: 路由判断
            local_count = self._count_local_sources(topic)
            logger.info(f"本地相关数据量: {local_count}")

            if local_count == 0:
                # 无本地数据 → Route C
                return self._route_claude_search(topic, reply_func, message_id)

            if local_count < 3:
                # 数据不足 → Route A (Claude 预处理)
                reply_func(message_id, f"📊 本地关于「{topic}」的数据较少（{local_count} 份），正在调用 Claude 汇总补充信息，请稍候...")

                preprocessor = ClaudePreprocessor(self.export_root)
                try:
                    summary_path = preprocessor.preprocess(topic)
                    logger.info(f"预处理摘要已保存: {summary_path}")
                    reply_func(message_id, f"✅ Claude 预处理完成，摘要已保存到 {summary_path}\n\n⏳ 正在上传到 NotebookLM 并执行深度分析...")
                except Exception as e:
                    logger.error(f"预处理失败: {e}")
                    reply_func(message_id, f"⚠️ Claude 预处理失败（{e}），继续执行原有流程...")
                    # 继续执行原有流程
            # 1. 整理文件 (清理上一次的导出，防止混淆)
            if self.export_root.exists():
                logger.info(f"清理导出分区... {self.export_root}")

            # 2. 解析参数
            template_key = None  # None = 自动检测
            actual_topic = topic
            include_videos = False
            video_limit = 5
            deep_mode = False
            auto_write = False

            remaining = topic
            if " --auto-write" in remaining:
                auto_write = True
                remaining = remaining.replace(" --auto-write", "")
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

            super_prompt = self._get_template_prompt(template_key, actual_topic)

            # [Research V8.0] Reflection & Grounding Check
            if auto_write and total_count > 0:
                logger.info("🛡️ 执行反思层校验 (Reflection Layer)...")
                # 这里我们抽样校验前 3 份素材
                sources_sample = [md.name for md in list(self.export_root.glob("**/*.md"))[:3]]
                is_aligned, reflection_msg = self._verify_relevance(actual_topic, sources_sample)
                
                if not is_aligned:
                    reply_func(message_id, f"⚠️ **执行方案对齐警告**\n\nAI 在反思检索结果时发现素材与主题「{actual_topic}」的对齐度较低。\n\n**原因**: {reflection_msg}\n\n💡 建议: 请尝试更具体的技术关键词，或启用 `--deep` 模式进行全网论文搜索。")
                    # 仍然发送卡片供用户手动检查，但不自动撰写
                    auto_write = False

            # [Research V4.0] End-to-End LLM Synthesis
            if auto_write:
                reply_func(message_id, f"📝 素材准备就绪 ({total_count}份)。正在使用内侧 AI 智库为您撰写端到端研究报告，请稍候约 1-2 分钟...")
                
                # 收集所有 Markdown 内容
                context_texts = []
                for md_file in self.export_root.glob("**/*.md"):
                    try:
                        with open(md_file, "r", encoding="utf-8") as f:
                            content = f.read()
                            # 限制单个文件长度防爆内存
                            context_texts.append(f"文献来源: {md_file.name}\n{content[:15000]}")
                    except Exception as e:
                        logger.warning(f"读取素材失败 {md_file}: {e}")
                
                full_context = "\n\n---\n\n".join(context_texts)
                final_prompt = f"{super_prompt}\n\n========== 检索到的参考素材 ==========\n{full_context}"
                
                logger.info(f"🚀 发起端到端撰写，上下文长度约 {len(final_prompt)} 字符...")
                success, final_report = llm_client.call(
                    role="research",
                    message=final_prompt,
                    system_prompt="你是知微系统内置的首席硬件系统架构师，请忽略 NotebookLM 专用的格式要求，直接输出最终的精美 Markdown 排版研报。字数不限，要深度且数据详实。",
                    timeout=300
                )
                
                if success:
                    report_path = self.export_root / f"AI_Report_{actual_topic[:10].replace(' ', '_')}.md"
                    with open(report_path, "w", encoding="utf-8") as rf:
                        rf.write(final_report)
                    reply_func(message_id, f"✅ **《{actual_topic}》自动研究报告已成稿** (保存在: {report_path})\n\n{final_report}")
                    return
                else:
                    reply_func(message_id, f"⚠️ 自动撰写研报失败，退回到素材包模式。错误原因: {final_report}")

            # 7. 发送常规结果卡片 (未开启 auto-write 或出错回退)
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

    def _verify_relevance(self, topic: str, source_names: list) -> tuple[bool, str]:
        """[Research V8.0] 校验检索素材与主题的相关性
        
        通过 LLM 进行反思，确保执行方案满足用户真实意图。
        """
        if not source_names:
            return False, "未检索到任何有效素材"

        prompt = f"""作为首席架构师，请对以下检索结果进行【相关性反思】：
研究主题: {topic}
检索出的素材样例: {', '.join(source_names)}

请判断这些素材是否足以支撑关于该主题的深度研究报告。
如果相关度很高，请回复：OK
如果相关度较低，请说明具体原因（一句话）。
"""
        try:
            # 内部调用同步版本（因为 execute 在线程池中运行）
            success, response = llm_client.call(
                role="research",
                message=prompt,
                system_prompt="你是一个严谨的研究计划审计员。只输出 OK 或简单的原因说明。",
                timeout=20
            )
            if success and response.strip().upper() == "OK":
                return True, ""
            return False, response.strip()
        except Exception as e:
            logger.error(f"相关性反思失败: {e}")
            return True, "" # 容错处理：调用失败则默认允许执行

# 单例
research_executor = ResearchReportExecutor()