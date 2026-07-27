#!/usr/bin/env python3
"""
视频洞察统一入口 v3.0
支持：抖音、B站、YouTube、TikTok 等全平台
全流程：视频获取 → ASR转录 → LLM分析 → 报告生成 → 缓存存储
依赖：yt-dlp、mlx-whisper、httpx（仅3个核心依赖）
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List

# 添加脚本目录到路径，确保能导入同级模块
SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

# 导入缓存模块
from video_cache import VideoCache, hash_url
# 导入抖音Cookie模块
from douyin_cookie import DouyinAPI, TokenManager
# 导入统一LLM客户端
sys.path.insert(0, str(Path.home() / "zhiwei-common"))
from llm_client import LLMClient, get_client


# ==================== 配置 ====================

# 通义千问API配置
DASHSCOPE_API_URL = "https://coding.dashscope.aliyuncs.com/v1/chat/completions"
DEFAULT_MODEL = "qwen3.7-plus"

# 工作目录
WORK_DIR = Path.home() / "Documents" / "video_insight_output"
# 缓存目录
CACHE_DIR = Path.home() / "Documents" / "video_insight_cache"

# LLM提示词
CHUNK_PROMPT = """你是专业的视频内容分析专家。分析以下视频转录片段，提取核心关键信息。

时间范围: {time_range}
转录内容: {transcript}

请严格按以下JSON格式返回（不要加额外内容）：
{{
  "title": "本片段主题，不超过20字",
  "bullets": ["关键要点1", "关键要点2", "关键要点3"],
  "key_quotes": ["最核心的原话1", "最核心的原话2"],
  "timestamp": "{start_time}"
}}

要求：
- bullets数量2-5个，每个不超过50字
- key_quotes数量1-2个，必须是原文直引
- 所有内容必须来自转录内容，不要编造"""

SUMMARY_PROMPT = """根据视频信息和分段分析结果，生成一份完整的视频分析报告。

视频信息: {video_info}
分段分析: {chunk_json_list}

请生成Markdown格式的完整报告，包含以下部分：
## 📹 一句话总览
（一句话概括视频核心观点，不超过50字）

## 💡 关键观点
（5-10个核心观点，每个用简短的一句话表述）

## 📋 重点章节
（按时间顺序列出重要内容时段，格式：[时分秒] 主题）

## 🎯 行动清单
（3-5条可执行的行动建议）

## 📊 视频数据
- 作者：{author}
- 时长：{duration}
- 点赞：{likes}
- 播放：{views}"""


def log(msg: str):
    """统一日志输出"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def get_api_key() -> str:
    """获取通义千问API Key"""
    # 优先环境变量
    for k in ["CODING_PLAN_API_KEY", "DASHSCOPE_API_KEY", "BAILIAN_API_KEY"]:
        v = os.environ.get(k)
        if v:
            return v
    # 其次.env文件
    for p in [
        Path.home() / "zhiwei-bot" / ".env",
        Path.home() / ".env",
        SCRIPT_DIR.parent / ".env",
    ]:
        if p.exists():
            for line in p.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    kn, kv = line.split('=', 1)
                    if kn.strip() in ["CODING_PLAN_API_KEY", "DASHSCOPE_API_KEY"]:
                        return kv.strip().strip('"\'')
    return ""


def detect_platform(url: str) -> str:
    """检测视频平台"""
    if "douyin.com" in url or "iesdouyin.com" in url:
        return "douyin"
    if "bilibili.com" in url or "b23.tv" in url:
        return "bilibili"
    if "youtube.com" in url or "youtu.be" in url:
        return "youtube"
    if "tiktok.com" in url:
        return "tiktok"
    return "unknown"


def format_time(seconds: int) -> str:
    """秒数转 分:秒 格式"""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


# ==================== 视频获取 ====================

def download_with_ytdlp(url: str, output_dir: Path, platform: str) -> Optional[Dict[str, Any]]:
    """用yt-dlp下载视频/音频（适用于B站/YouTube/TikTok）"""
    output_dir.mkdir(parents=True, exist_ok=True)
    audio_path = output_dir / "audio.mp3"

    log(f"yt-dlp下载中: {url[:60]}...")
    try:
        cmd = [
            "yt-dlp",
            "--no-playlist",
            "--extract-audio",
            "--audio-format", "mp3",
            "--audio-quality", "0",
            "-o", str(audio_path),
            url
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300
        )
        if result.returncode != 0:
            log(f"yt-dlp下载失败: {result.stderr[:200]}")
            return None

        if not audio_path.exists():
            log("下载的音频文件不存在")
            return None

        return {
            "audio_path": audio_path,
            "video_info": {
                "platform": platform,
                "url": url,
                "title": "",
                "author": "",
                "duration": 0,
            }
        }
    except subprocess.TimeoutExpired:
        log("下载超时")
        return None
    except Exception as e:
        log(f"下载异常: {e}")
        return None


def download_douyin(url: str, output_dir: Path, custom_cookie: str = None) -> Optional[Dict[str, Any]]:
    """下载抖音视频（使用Cookie模块）"""
    output_dir.mkdir(parents=True, exist_ok=True)

    api = DouyinAPI(custom_cookie=custom_cookie)
    # 获取视频信息
    video_info = api.get_video_info(url)
    if not video_info or not video_info.get("video_url"):
        log("获取抖音视频信息失败，请尝试提供自定义Cookie")
        return None

    video_url = video_info["video_url"]
    aweme_id = video_info.get("aweme_id", "unknown")

    log(f"抖音视频: {video_info.get('title', '')[:40]} | 作者: {video_info.get('author', '')}")

    # 下载视频
    video_path = output_dir / f"{aweme_id}.mp4"
    audio_path = output_dir / "audio.wav"

    try:
        with httpx.Client(timeout=120) as client:
            r = client.get(video_url, headers=api._get_headers())
            r.raise_for_status()
            video_path.write_bytes(r.content)

        # 提取音频
        subprocess.run([
            "ffmpeg", "-y", "-i", str(video_path),
            "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
            str(audio_path)
        ], capture_output=True, check=True, timeout=60)

        duration_sec = (video_info.get("duration", 0) // 1000)

        return {
            "audio_path": audio_path,
            "video_info": {
                "platform": "douyin",
                "url": url,
                "aweme_id": aweme_id,
                "title": video_info.get("title", ""),
                "author": video_info.get("author", ""),
                "duration": duration_sec,
                "like_count": video_info.get("like_count", 0),
                "comment_count": video_info.get("comment_count", 0),
                "play_count": video_info.get("play_count", 0),
                "cover_url": video_info.get("cover_url", ""),
            }
        }
    except Exception as e:
        log(f"抖音下载失败: {e}")
        return None


def download_video(url: str, output_dir: Path, custom_cookie: str = None) -> Optional[Dict[str, Any]]:
    """统一视频下载入口，自动选择下载方式"""
    platform = detect_platform(url)
    log(f"检测到平台: {platform}")

    if platform == "douyin":
        return download_douyin(url, output_dir, custom_cookie)
    else:
        return download_with_ytdlp(url, output_dir, platform)


# ==================== ASR转录 ====================

def transcribe_audio(audio_path: Path, output_dir: Path) -> Optional[Dict[str, Any]]:
    """
    音频转录，优先用mlx-whisper（Mac原生加速），降级用openai-whisper
    返回: {transcript: list, language: str, duration: float} 或 None
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    transcript_file = output_dir / "transcript.json"

    log(f"开始转录: {audio_path.name}")
    t0 = time.time()
    asr_engine = "mlx_whisper"

    # 尝试用mlx-whisper
    try:
        from mlx_whisper import transcribe as mlx_transcribe
        result = mlx_transcribe(str(audio_path), word_timestamps=False)
    except ImportError:
        # 降级用openai-whisper
        log("⚠️ mlx-whisper未安装，降级用openai-whisper（CPU模式，较慢）")
        try:
            import whisper
            asr_engine = "openai_whisper"
            model = whisper.load_model("base")
            result = model.transcribe(str(audio_path))
        except ImportError:
            log("❌ 没有可用的ASR引擎，请安装mlx-whisper或openai-whisper")
            return None
    except Exception as e:
        log(f"转录失败: {e}")
        return None

    # 处理结果
    try:
        if asr_engine == "mlx_whisper":
            text = result.get('text', '')
            language = result.get('language', 'zh')
            segments_raw = result.get('segments', [])
        else:  # openai-whisper
            text = result.get('text', '')
            language = result.get('language', 'zh')
            segments_raw = result.get('segments', [])

        # 转换为标准格式
        transcript = []
        for seg in segments_raw:
            transcript.append({
                "start": round(seg.get('start', 0), 2),
                "end": round(seg.get('end', 0), 2),
                "text": seg.get('text', '').strip()
            })

        # 保存转录结果
        transcript_file.write_text(json.dumps(transcript, ensure_ascii=False, indent=2))

        duration = time.time() - t0
        word_count = len(text)
        log(f"转录完成: {len(transcript)}段, {word_count}字, 耗时{duration:.1f}s, 引擎:{asr_engine}")

        # 质量校验：字数 < 时长*2 视为低质量
        total_duration = sum(s['end'] - s['start'] for s in transcript) if transcript else 0
        if total_duration > 0 and word_count < total_duration * 2:
            log(f"⚠️ 转录质量偏低（{word_count}字/{total_duration:.0f}s），建议检查音频")

        return {
            "transcript": transcript,
            "language": language,
            "duration": duration,
            "word_count": word_count,
            "asr_engine": asr_engine
        }
    except Exception as e:
        log(f"转录结果处理失败: {e}")
        return None


# ==================== 素材归档 ====================

def _archive_files(url: str, work_dir: Path, audio_path: Path, report_path: Path,
                   video_info: Dict, platform: str, url_hash: str) -> Optional[Path]:
    """
    归档所有原始素材到统一目录
    返回归档目录路径，失败返回None
    """
    try:
        # 生成归档目录：~/VideoArchive/平台/日期-视频ID/
        today = datetime.now().strftime("%Y-%m-%d")
        video_id = video_info.get("aweme_id") or video_info.get("id") or url_hash[:12]
        archive_base = Path.home() / "VideoArchive" / platform / today / video_id
        archive_base.mkdir(parents=True, exist_ok=True)

        # 复制音频文件
        if audio_path.exists():
            import shutil
            audio_dest = archive_base / "audio.wav"
            shutil.copy2(audio_path, audio_dest)

        # 复制转录JSON
        transcript_src = work_dir / "transcript.json"
        if transcript_src.exists():
            import shutil
            transcript_dest = archive_base / "transcript.json"
            shutil.copy2(transcript_src, transcript_dest)

        # 复制报告
        if report_path.exists():
            import shutil
            report_dest = archive_base / "report.md"
            shutil.copy2(report_path, report_dest)

        # 生成metadata.json
        metadata = {
            "url": url,
            "platform": platform,
            "title": video_info.get("title", ""),
            "author": video_info.get("author", ""),
            "duration": video_info.get("duration", 0),
            "like_count": video_info.get("like_count", 0),
            "comment_count": video_info.get("comment_count", 0),
            "play_count": video_info.get("play_count", 0),
            "cover_url": video_info.get("cover_url", ""),
            "archived_at": datetime.now().isoformat(),
            "url_hash": url_hash
        }
        metadata_path = archive_base / "metadata.json"
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')

        # 更新全局索引
        _update_global_index(metadata, archive_base)

        return archive_base
    except Exception as e:
        log(f"归档失败: {e}")
        return None


def _update_global_index(metadata: Dict, archive_dir: Path):
    """更新全局归档索引"""
    index_path = Path.home() / "VideoArchive" / "index.json"
    index = []
    if index_path.exists():
        try:
            index = json.loads(index_path.read_text(encoding='utf-8'))
        except Exception:
            index = []
    # 添加新索引项
    index.append({
        "url": metadata.get("url"),
        "title": metadata.get("title"),
        "platform": metadata.get("platform"),
        "archived_at": metadata.get("archived_at"),
        "archive_dir": str(archive_dir)
    })
    # 最多保留1000条索引
    if len(index) > 1000:
        index = index[-1000:]
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding='utf-8')


# ==================== LLM分析 ====================

def call_llm(prompt: str, max_tokens: int = 2000, use_schema: bool = False,
             schema: Dict = None, task_type: str = "basic") -> Optional[str]:
    """调用统一LLM客户端，支持结构化输出"""
    try:
        client = get_client()
        result = client.call(task_type, prompt, schema=schema, max_tokens=max_tokens)
        if result.get("success"):
            return result["content"]
        else:
            log(f"LLM调用失败: {result.get('error', '未知错误')}")
            return None
    except Exception as e:
        log(f"LLM调用异常: {e}")
        return None


def chunk_segments(segments: List[Dict], chunk_duration: int = 300) -> List[Dict]:
    """按时间分块，默认300秒一块"""
    if not segments:
        return []

    chunks = []
    current_chunk = []
    chunk_start = segments[0]["start"]

    for seg in segments:
        current_chunk.append(seg)
        if seg["end"] - chunk_start >= chunk_duration:
            chunks.append({
                "start": chunk_start,
                "end": seg["end"],
                "segments": current_chunk
            })
            current_chunk = []
            chunk_start = seg["end"]

    # 剩余部分
    if current_chunk:
        chunks.append({
            "start": chunk_start,
            "end": current_chunk[-1]["end"],
            "segments": current_chunk
        })

    return chunks


def analyze_chunk(chunk: Dict, index: int, total: int) -> Optional[Dict]:
    """分析单个视频片段"""
    time_range = f"{format_time(int(chunk['start']))}-{format_time(int(chunk['end']))}"
    transcript_text = " ".join([s["text"] for s in chunk["segments"]])

    # 截断过长内容，避免超过上下文窗口
    if len(transcript_text) > 4000:
        transcript_text = transcript_text[:4000]

    log(f"  分析片段 {index+1}/{total}: {time_range}")

    prompt = CHUNK_PROMPT.format(
        time_range=time_range,
        transcript=transcript_text,
        start_time=format_time(int(chunk['start']))
    )

    result = call_llm(prompt, max_tokens=1500)
    if not result:
        return None

    try:
        return json.loads(result)
    except json.JSONDecodeError:
        log(f"  ⚠️ 片段{index+1} JSON解析失败")
        return {
            "title": f"片段{index+1}",
            "bullets": [],
            "key_quotes": [],
            "timestamp": format_time(int(chunk['start']))
        }


def generate_report(video_info: Dict, chunks_analysis: List[Dict],
                    segments: List[Dict], include_transcript: bool = False,
                    url: str = None, cache: VideoCache = None) -> str:
    """生成完整的视频分析报告，包含三层深度分析"""
    log("生成汇总报告...")

    info_str = (
        f"标题:{video_info.get('title','?')} | "
        f"作者:{video_info.get('author','?')} | "
        f"时长:{format_time(video_info.get('duration',0))} | "
        f"点赞:{video_info.get('like_count',0)} | "
        f"播放:{video_info.get('play_count',0)}"
    )

    prompt = SUMMARY_PROMPT.format(
        video_info=info_str,
        chunk_json_list=json.dumps(chunks_analysis, ensure_ascii=False),
        author=video_info.get('author', '?'),
        duration=format_time(video_info.get('duration', 0)),
        likes=video_info.get('like_count', 0),
        views=video_info.get('play_count', 0)
    )

    report = call_llm(prompt, max_tokens=4000)
    if not report:
        return "报告生成失败"

    # === 第二层：深度观点分析 ===
    log("生成深度观点分析...")
    viewpoints = _generate_deep_viewpoints(chunks_analysis, segments, url, cache)
    if viewpoints:
        report += "\n\n## 🧠 深度观点分析\n"
        for vp in viewpoints:
            report += f"\n### {vp.get('viewpoint', '')}\n"
            report += f"- 支撑逻辑：{vp.get('support_logic', '')}\n"
            report += f"- 适用范围：{vp.get('applicability', '')}\n"
            report += f"- 局限性：{vp.get('limitations', '')}\n"
            report += f"- 置信度：{vp.get('confidence', 0):.0%}\n"

    # === 第三层：技术知识关联图谱 ===
    log("生成技术知识关联图谱...")
    knowledge = _generate_knowledge_graph(segments, url, cache)
    if knowledge:
        report += "\n\n## 🔬 技术知识百科\n"
        for term in knowledge.get("tech_terms", []):
            report += f"\n### {term.get('term', '')}\n"
            report += f"{term.get('definition', '')}\n"
            if term.get("connections"):
                report += f"- 关联技术：{', '.join(term.get('connections', []))}\n"
        report += "\n## 🕸️ 知识关联图谱\n"
        report += "```\n"
        for edge in knowledge.get("knowledge_graph", {}).get("edges", []):
            report += f"{edge.get('from', '')} --[{edge.get('relation', '')}]--> {edge.get('to', '')}\n"
        report += "```\n"
        report += "\n## 📚 延伸学习推荐\n"
        for term in knowledge.get("tech_terms", [])[:5]:
            if term.get("related_papers"):
                report += f"- **{term.get('term', '')}相关论文**：{', '.join(term.get('related_papers', [])[:3])}\n"
            if term.get("related_projects"):
                report += f"- **{term.get('term', '')}相关项目**：{', '.join(term.get('related_projects', [])[:3])}\n"

    # 追加转录内容
    if include_transcript and segments:
        report += "\n\n---\n## 📝 全量转录\n"
        for seg in segments:
            report += f"[{format_time(int(seg['start']))}] {seg['text']}\n"

    return report


def _generate_deep_viewpoints(chunks_analysis: List[Dict], segments: List[Dict],
                              url: str = None, cache: VideoCache = None) -> List[Dict]:
    """第二层：深度观点分析"""
    try:
        # 构建所有观点的文本
        all_viewpoints = []
        for chunk in chunks_analysis:
            if chunk.get("title") and chunk.get("bullets"):
                all_viewpoints.append({
                    "title": chunk.get("title"),
                    "bullets": chunk.get("bullets", []),
                    "timestamp": chunk.get("timestamp", "")
                })

        if not all_viewpoints:
            return []

        # 调用LLM做深度分析
        prompt = f"""以下是视频中提取的关键观点，请对每个观点做深度分析，输出JSON数组：
{json.dumps(all_viewpoints, ensure_ascii=False, indent=2)}

对每个观点输出：
- viewpoint: 观点原文
- support_logic: 这个观点的支撑论据/逻辑是什么
- applicability: 适用场景/范围
- limitations: 局限性/前提条件
- related_topics: 相关的话题列表
- confidence: 观点的置信度0-1（基于内容完整性）"""

        schema = {
            "name": "deep_viewpoints",
            "schema": {
                "type": "object",
                "properties": {
                    "deep_viewpoints": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "viewpoint": {"type": "string"},
                                "support_logic": {"type": "string"},
                                "applicability": {"type": "string"},
                                "limitations": {"type": "string"},
                                "related_topics": {"type": "array", "items": {"type": "string"}},
                                "confidence": {"type": "number"}
                            },
                            "required": ["viewpoint", "support_logic", "applicability", "limitations"]
                        }
                    }
                },
                "required": ["deep_viewpoints"]
            }
        }

        result = call_llm(prompt, max_tokens=4000, schema=schema, task_type="deep")
        if not result:
            return []

        # 解析结果
        try:
            viewpoints = json.loads(result) if isinstance(result, str) else result
            # 兼容多种返回格式：纯数组/对象包含deep_viewpoints字段
            if isinstance(viewpoints, dict):
                viewpoints = viewpoints.get("deep_viewpoints", [])
            if not isinstance(viewpoints, list):
                log(f"⚠️ 深度观点返回格式错误: {type(viewpoints)}")
                return []
        except json.JSONDecodeError as e:
            log(f"⚠️ 深度观点JSON解析失败: {e}")
            return []

        # 保存到数据库
        if url and cache and viewpoints:
            for vp in viewpoints:
                cache.save_deep_viewpoint(
                    url=url,
                    viewpoint=vp.get("viewpoint", ""),
                    support_logic=vp.get("support_logic", ""),
                    applicability=vp.get("applicability", ""),
                    limitations=vp.get("limitations", ""),
                    related_topics=vp.get("related_topics", []),
                    confidence=float(vp.get("confidence", 0.5))
                )
        return viewpoints
    except Exception as e:
        log(f"深度观点分析失败: {e}")
        return []


def _generate_knowledge_graph(segments: List[Dict], url: str = None,
                              cache: VideoCache = None) -> Optional[Dict]:
    """第三层：技术知识关联图谱"""
    try:
        # 取前3000字的转录内容用于提取技术术语
        all_text = " ".join([s.get("text", "") for s in segments])[:3000]
        if not all_text.strip():
            return None

        prompt = f"""从以下视频转录内容中提取所有提到的技术术语，生成知识关联图谱：
{all_text}

输出JSON格式：
{{
  "tech_terms": [
    {{
      "term": "技术术语",
      "definition": "简明定义（50字内）",
      "related_papers": ["相关论文标题/链接"],
      "related_projects": ["相关开源项目"],
      "learning_resources": ["推荐学习资料/链接"],
      "connections": ["关联的其他技术术语"]
    }}
  ],
  "knowledge_graph": {{
    "nodes": ["技术术语列表"],
    "edges": [
      {{"from": "术语A", "to": "术语B", "relation": "依赖/关联/衍生/同类"}}
    ]
  }}
}}

要求：
- 至少提取3个技术术语，最多10个
- 每个术语的定义要简明准确
- edges要体现术语之间的真实逻辑关系"""

        schema = {
            "name": "knowledge_graph",
            "schema": {
                "type": "object",
                "properties": {
                    "tech_terms": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "term": {"type": "string"},
                                "definition": {"type": "string"},
                                "related_papers": {"type": "array", "items": {"type": "string"}},
                                "related_projects": {"type": "array", "items": {"type": "string"}},
                                "learning_resources": {"type": "array", "items": {"type": "string"}},
                                "connections": {"type": "array", "items": {"type": "string"}}
                            },
                            "required": ["term", "definition", "connections"]
                        }
                    },
                    "knowledge_graph": {
                        "type": "object",
                        "properties": {
                            "nodes": {"type": "array", "items": {"type": "string"}},
                            "edges": {"type": "array"}
                        }
                    }
                },
                "required": ["tech_terms", "knowledge_graph"]
            }
        }

        result = call_llm(prompt, max_tokens=4000, schema=schema, task_type="deep")
        if not result:
            return None

        try:
            knowledge = json.loads(result) if isinstance(result, str) else result
        except json.JSONDecodeError:
            log("⚠️ 知识图谱JSON解析失败")
            return None
        # 保存到数据库
        if url and cache and knowledge.get("tech_terms"):
            for term in knowledge.get("tech_terms", []):
                cache.save_knowledge_term(
                    url=url,
                    term=term.get("term", ""),
                    definition=term.get("definition", ""),
                    related_terms=term.get("connections", []),
                    related_papers=term.get("related_papers", []),
                    related_projects=term.get("related_projects", []),
                    learning_resources=term.get("learning_resources", [])
                )
        return knowledge
    except Exception as e:
        log(f"知识图谱生成失败: {e}")
        return None


# ==================== 主流程 ====================

def process_video(url: str, include_transcript: bool = False,
                  force: bool = False, custom_cookie: str = None) -> Optional[str]:
    """
    处理单个视频的完整流程
    返回: 生成的报告内容，失败返回None
    """
    log(f"开始处理: {url[:80]}")

    # 初始化缓存
    with VideoCache() as cache:
        # 检查是否已处理
        if not force and cache.is_url_processed(url):
            log("✅ 该视频已处理过，直接返回缓存结果")
            result = cache.get_processed_result(url)
            if result and result.get("report_path"):
                report_path = Path(result["report_path"])
                if report_path.exists():
                    return report_path.read_text(encoding='utf-8')

        # 创建处理记录
        platform = detect_platform(url)
        url_hash = cache.create_record(url, platform)
        cache.update_status(url_hash, "processing")

        # 创建工作目录
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        work_dir = WORK_DIR / ts
        work_dir.mkdir(parents=True, exist_ok=True)

        try:
            # 1. 下载视频
            log("步骤1/4: 下载视频...")
            download_result = download_video(url, work_dir, custom_cookie=custom_cookie)
            if not download_result:
                cache.update_status(url_hash, "fail", error_message="视频下载失败")
                return None

            audio_path = download_result["audio_path"]
            video_info = download_result["video_info"]

            # 更新记录信息
            cache.update_status(url_hash, "processing")

            # 2. 转录
            log("步骤2/4: ASR转录...")
            # 检查转录缓存
            cached_transcript = cache.get_transcript(url)
            if cached_transcript and not force:
                log("✅ 使用缓存的转录结果")
                transcript_data = {
                    "transcript": cached_transcript["transcript_json"],
                    "language": cached_transcript["language"],
                    "duration": cached_transcript["asr_duration"],
                    "word_count": cached_transcript["word_count"],
                    "asr_engine": cached_transcript["asr_engine"]
                }
            else:
                transcript_data = transcribe_audio(audio_path, work_dir)
                if not transcript_data:
                    cache.update_status(url_hash, "fail", error_message="转录失败")
                    return None
                # 保存转录缓存
                cache.save_transcript(
                    url, transcript_data["transcript"],
                    transcript_data["language"],
                    transcript_data["asr_engine"],
                    transcript_data["duration"]
                )

            segments = transcript_data["transcript"]

            # 3. LLM分析
            log("步骤3/4: LLM分析...")
            duration = video_info.get("duration", 0)
            if duration == 0:
                duration = sum(s['end'] - s['start'] for s in segments)

            # 短视频不分段
            if duration < 180:
                log("短视频，直接整体分析")
                chunks = [{"start": 0, "end": duration, "segments": segments}]
            else:
                chunks = chunk_segments(segments, chunk_duration=300)

            log(f"共{len(chunks)}个分析片段")
            chunks_analysis = []
            for i, chunk in enumerate(chunks):
                analysis = analyze_chunk(chunk, i, len(chunks))
                if analysis:
                    chunks_analysis.append(analysis)

            # 4. 生成报告
            log("步骤4/4: 生成报告...")
            report = generate_report(video_info, chunks_analysis, segments, include_transcript, url=url, cache=cache)

            # 保存报告
            safe_title = re.sub(r'[\\/*?:"<>|\n\r\t#@]', "", video_info.get('title', ''))[:30]
            report_path = work_dir / f"report_{safe_title}.md"
            report_path.write_text(report, encoding='utf-8')

            # 5. 归档所有原始素材
            log("步骤5/5: 归档素材...")
            archive_dir = _archive_files(url, work_dir, audio_path, report_path, video_info, platform, url_hash)
            if archive_dir:
                log(f"✅ 归档完成: {archive_dir}")

            # 更新缓存状态
            cache.update_status(url_hash, "success", report_path=str(report_path),
                                archive_path=str(archive_dir) if archive_dir else None,
                                audio_path=str(audio_path),
                                transcript_path=str(work_dir / "transcript.json") if (work_dir / "transcript.json").exists() else None)

            log(f"✅ 处理完成! 报告: {report_path}")
            return report

        except Exception as e:
            log(f"❌ 处理异常: {e}")
            cache.update_status(url_hash, "fail", error_message=str(e))
            return None


def main():
    parser = argparse.ArgumentParser(description="视频洞察 v3.0 - 全平台视频分析")
    parser.add_argument("--url", required=True, help="视频URL（支持抖音/B站/YouTube/TikTok）")
    parser.add_argument("--include-transcript", action="store_true", help="报告中包含完整转录")
    parser.add_argument("--force", action="store_true", help="强制重新处理，忽略缓存")
    parser.add_argument("--cookie", default=None, help="自定义抖音Cookie（可选）")
    args = parser.parse_args()

    report = process_video(
        url=args.url,
        include_transcript=args.include_transcript,
        force=args.force,
        custom_cookie=args.cookie
    )

    if report:
        print("\n" + "="*60)
        print(report)
        print("="*60)
    else:
        print("❌ 处理失败，请检查日志")
        sys.exit(1)


if __name__ == "__main__":
    main()
