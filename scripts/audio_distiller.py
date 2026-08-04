#!/usr/bin/env python3
"""音频文件知识蒸馏器（2026-08-02）

飞书发送的录音文件（m4a/mp3/wav/...）→ 转写（mimo-asr 云端首选，
本地 MLX Whisper 兜底，与飞书语音消息同一策略）→ 复用视频蒸馏链路
（KnowledgeDistiller 两阶段 LLM + MarkdownWriter 同模板渲染）
→ Obsidian 笔记(Inbox) → RAG 增量入库。
由 media_handler.process_audio_file 以子进程调用。

用法: python3 audio_distiller.py --audio <file.m4a> --output-dir <dir>
重蒸馏: python3 audio_distiller.py --transcript-file <已有转写.txt>
          [--title 标题] [--content-type business_insight] --output-dir <dir>
成功输出 "✅ Done! Output: <md路径>"（与视频/PDF 蒸馏器协议一致）；
失败向 stderr 输出 {"error_type": ..., "error_message": ...} JSON。
"""
import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # scripts/ 目录

from douyin_distiller import (  # noqa: E402
    AppConfig, VideoInfo, TranscriptResult,
    MimoASRTranscriber, LocalMLXWhisperTranscriber,
    KnowledgeDistiller, MarkdownWriter, _trigger_rag_ingest,
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("audio_distiller")

MIN_CHARS = 50  # 转写结果低于此长度视为失败


def _fail(error_type: str, message: str) -> int:
    print(json.dumps({"error_type": error_type, "error_message": message},
                     ensure_ascii=False), file=sys.stderr)
    return 1


def transcribe(audio_path: Path, cfg: AppConfig):
    """mimo-asr 云端首选（快且准），本地 MLX Whisper 兜底（不依赖云端 key）。

    与 media_handler.transcribe_audio 同一策略，搬到这里是为了让本脚本
    作为独立子进程运行（media_handler 依赖 bot 运行时全局对象，无法 import）。
    """
    # 1. mimo-asr 云端首选
    if getattr(cfg, "mimo_api_key", ""):
        try:
            tr = MimoASRTranscriber(cfg.mimo_api_key, cfg.mimo_api_base, cfg.mimo_asr_model)
            res = tr.transcribe(audio_path)
            if res and res.full_text:
                logger.info(f"mimo-asr 转写成功: {len(res.full_text)} 字")
                return res.full_text, "mimo_asr"
            logger.warning("mimo-asr 空结果, 降级本地 MLX")
        except Exception as e:
            logger.warning(f"mimo-asr 失败: {e}, 降级本地 MLX")

    # 2. 本地 MLX Whisper 兜底
    try:
        local = LocalMLXWhisperTranscriber(getattr(cfg, "local_asr_model", "small"))
        if local.is_available():
            res = local.transcribe(audio_path)
            if res and res.full_text:
                logger.info(f"本地 MLX 转写成功: {len(res.full_text)} 字")
                return res.full_text, "local_asr"
    except Exception as e:
        logger.error(f"本地 MLX 也失败: {e}")

    return None, None


def main() -> int:
    parser = argparse.ArgumentParser(description="音频文件知识蒸馏器")
    parser.add_argument("--audio", help="音频文件路径")
    parser.add_argument("--transcript-file", help="复用已有转写文本（跳过 ASR，用于重蒸馏）")
    parser.add_argument("--content-type", help="强制内容类型（覆盖 Stage1 自动分类，如 business_insight）")
    parser.add_argument("--title", help="标题提示（缺省取音频名/转写目录名）")
    parser.add_argument("--output-dir", required=True, help="笔记输出目录")
    args = parser.parse_args()

    audio_path = Path(args.audio).expanduser() if args.audio else None
    transcript_file = Path(args.transcript_file).expanduser() if args.transcript_file else None
    if not transcript_file and not (audio_path and audio_path.exists()):
        return _fail("module_error", "音频文件不存在，且未提供 --transcript-file")
    if transcript_file and not transcript_file.exists():
        return _fail("module_error", f"转写文件不存在: {transcript_file}")

    # 标题提示：显式 --title > 音频文件名 > 转写所在 Assets 目录名（含 日期_标题）
    title_hint = (args.title
                  or (audio_path.stem if audio_path else "")
                  or (transcript_file.parent.name if transcript_file else "未知录音"))

    # 1. 转写（或复用缓存）
    cfg = AppConfig()
    if transcript_file:
        text = transcript_file.read_text(errors="ignore")
        source = "transcript_cache"
        logger.info(f"Step 1: 复用已有转写 {transcript_file.name}: {len(text)} 字符")
    else:
        logger.info(f"Step 1: 转写音频: {audio_path.name}")
        text, source = transcribe(audio_path, cfg)
    if not text or len(text.strip()) < MIN_CHARS:
        return _fail("module_error", "音频转写失败（mimo-asr 与本地 MLX 均未产出有效文本）")
    logger.info(f"转写完成({source}): {len(text)} 字符")

    # 2. 复用视频蒸馏链路（两阶段 LLM + 同模板渲染）
    logger.info("Step 2: 两阶段知识蒸馏（复用视频链路）")
    video_info = VideoInfo(
        original_url=f"audio://{title_hint}",
        resolved_url="",
        platform="audio",
        title=title_hint,
        author=title_hint,
        duration=0,
    )
    transcript = TranscriptResult(full_text=text, source=source)
    distiller = KnowledgeDistiller(cfg)
    if args.content_type:
        # 强制内容类型：Stage1 仍跑（实体/纠错），但分类结果用指定的。
        # 2026-08-02 背景：Stage1 瞬挂会静默降级为 general 模板，笔记变薄；
        # 重蒸馏时人工指定类型可绕开（如电话访谈 → business_insight）。
        _orig_stage1 = distiller._stage1_clean_and_classify

        def _forced_stage1(text):
            r = _orig_stage1(text)
            r["content_type"] = args.content_type
            return r

        distiller._stage1_clean_and_classify = _forced_stage1
        logger.info(f"内容类型强制为: {args.content_type}")
    try:
        knowledge = distiller.distill(video_info, transcript)
    except Exception as e:
        return _fail("module_error", f"LLM 蒸馏失败: {e}")

    # 3. 写入 Obsidian（output_dir 由调用方指定，当前为 Inbox）
    out_dir = Path(args.output_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = MarkdownWriter(out_dir).write(
        video_info, transcript, knowledge, noise_tags=[])

    # 修正 frontmatter 类型标记（复用视频模板，type 字段需区分开）
    content = output_path.read_text(encoding="utf-8")
    content = content.replace("type: video_distill", "type: audio_distill", 1)
    output_path.write_text(content, encoding="utf-8")

    # 4. RAG 增量入库（A/B 级，fire-and-forget）
    _trigger_rag_ingest(output_path)

    logger.info(f"✅ Done! Output: {output_path}")
    print(f"✅ Done! Output: {output_path}")
    return 0


if __name__ == "__main__":
    exit(main())
