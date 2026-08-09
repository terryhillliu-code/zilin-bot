"""ComfyUI 生图异步链路（2026-08-09 批 1）

链路：nl_router image_gen 意图 → handle_image_gen_async 先回执 →
独立 daemon 线程（不进 ws_client 主池）内 Semaphore(2) 限并发 →
gpu_offload.submit("comfyui", mode="async") 派发笔记本 ComfyUI →
fetch_output 拉回 PNG → reply_image 会话内回图。

约束：
- feishu_api.py 零改动：只读取运行时注入的 feishu_api.client；
- 失败如实回执（Mac 本无生成能力，不伪造）；
- 每次调用落一行 ~/logs/gpu_offload_metrics.jsonl（批 2/3 台账数据源）。
"""
import json
import random
import threading
import time
import traceback
from pathlib import Path

from zhiwei_common import gpu_offload

# 并发上限：GPU 单槽 + 冷却 30s，2 并发已是队列极限
_GEN_SEM = threading.Semaphore(2)

_WORKFLOW_PATH = Path(__file__).resolve().parent.parent / "workflows" / "flux_txt2img.json"
_METRICS_PATH = Path.home() / "logs" / "gpu_offload_metrics.jsonl"
_METRICS_LOCK = threading.Lock()

SUBMIT_TIMEOUT = 600   # gpu_offload worker 硬上限 600s
TOTAL_TIMEOUT = 900    # 含排队/冷却的总预算 15min


def _log_metrics(status: str, duration_s: float, transfer_mode=None, job_id=None):
    """追加一行台账（append 单行写，批 2/3 前置数据源）"""
    rec = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "worker": "comfyui",
        "status": status,
        "duration_s": round(duration_s, 1),
        "transfer_mode": transfer_mode,
        "job_id": job_id,
    }
    try:
        _METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _METRICS_LOCK:
            with open(_METRICS_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"⚠️ gpu_offload_metrics 落账失败: {e}")


def _build_workflow_file(prompt: str) -> str:
    """模板 workflow + 用户 prompt 填充 + 随机 seed，写临时文件返回路径。

    只替换正向 prompt 文本（节点 6）与 seed（节点 3），不做复杂参数解析。
    随机 seed 同时避免 ComfyUI 节点缓存导致出同一张图。
    """
    wf = json.loads(_WORKFLOW_PATH.read_text(encoding="utf-8"))
    wf["6"]["inputs"]["text"] = prompt[:2000]
    wf["3"]["inputs"]["seed"] = random.randint(0, 2**32 - 1)
    tmp = Path("/tmp") / f"zhiwei_flux_{int(time.time()*1000)}_{random.randint(1000,9999)}.json"
    tmp.write_text(json.dumps(wf, ensure_ascii=False), encoding="utf-8")
    return str(tmp)


def _transfer_mode() -> str:
    """记录本次链路实际走的传输通道（与 gpu_offload 内部探测逻辑一致）"""
    try:
        return "direct_ssh" if gpu_offload._direct_reachable() else "oss"
    except Exception:
        return "unknown"


def handle_image_gen_async(prompt: str, user_id: str, message_id: str, ctx):
    """生图入口：先回执，再起独立 daemon 线程执行（禁止占用消息主池）。"""
    ctx.reply_message(
        message_id,
        "🎨 开始生成图片（FLUX·1024×1024）...\n\n"
        "⏳ 预计 2-4 分钟（含 GPU 排队与冷却），完成后自动回复图片")
    t = threading.Thread(
        target=_gen_and_reply,
        args=(prompt, user_id, message_id, ctx),
        name=f"image-gen-{int(time.time())}",
        daemon=True)
    t.start()


def _gen_and_reply(prompt: str, user_id: str, message_id: str, ctx):
    t0 = time.time()
    job_id = None
    wf_path = None
    acquired = False
    try:
        wf_path = _build_workflow_file(prompt)
        transfer_mode = _transfer_mode()

        # ⭐ 2026-08-09: 总预算改剩余预算语义——排队等待与 submit 共享
        # TOTAL_TIMEOUT 一份预算(原先两段串联最坏 ~30min)
        remaining = TOTAL_TIMEOUT - (time.time() - t0)
        acquired = _GEN_SEM.acquire(timeout=max(0, remaining))
        if not acquired:
            _log_metrics("queued_timeout", time.time() - t0, transfer_mode, job_id)
            ctx.reply_message(message_id, "❌ 生图队列已排满（等待耗尽总预算 15 分钟），请稍后再试。")
            return

        # submit 用剩余预算, 下限 60s(低于此出图必超时不如快速失败), 上限 SUBMIT_TIMEOUT
        submit_timeout = min(SUBMIT_TIMEOUT, max(60, int(TOTAL_TIMEOUT - (time.time() - t0))))
        result = gpu_offload.submit(
            "comfyui", inputs=[wf_path],
            params={"timeout": submit_timeout},
            mode="async", timeout=submit_timeout)
        job_id = result.get("job_id") if isinstance(result, dict) else None

        if result is None:
            _log_metrics("submit_failed", time.time() - t0, transfer_mode, job_id)
            ctx.reply_message(
                message_id,
                "❌ 生图失败：GPU 工作站不在线或派发超时。"
                "本机无生成能力，请稍后重试。")
            return
        status = result.get("status")
        if status == "busy":
            _log_metrics("busy", time.time() - t0, transfer_mode, job_id)
            ctx.reply_message(message_id, "❌ GPU 正在执行其他任务（单槽），请稍后重试。")
            return
        if status != "done":
            err = (result.get("error") or "未知错误")[:300]
            _log_metrics(status or "failed", time.time() - t0, transfer_mode, job_id)
            ctx.reply_message(message_id, f"❌ 生图失败（{status}）：{err}")
            return

        files = (result.get("outputs_meta") or {}).get("files") or []
        if not files:
            _log_metrics("no_output", time.time() - t0, transfer_mode, job_id)
            ctx.reply_message(message_id, "❌ 生图完成但未找到产出文件，请重试。")
            return

        dest = Path("/tmp") / f"zhiwei_gen_{job_id}_{files[0]}"
        gpu_offload.fetch_output(result, f"out/{files[0]}", str(dest))
        dur = time.time() - t0
        _log_metrics("done", dur, transfer_mode, job_id)

        if reply_image(message_id, str(dest)):
            # ⭐ 2026-08-09: 回图成功后清理 /tmp 产物, 避免累积
            try:
                dest.unlink(missing_ok=True)
            except Exception:
                pass
        else:
            # 上传失败留存本地文件(文案已告知路径, 便于手动取回)
            ctx.reply_message(
                message_id,
                f"🎨 图片已生成（{dur/60:.1f} 分钟）但飞书上传失败。\n"
                f"本机留存路径：{dest}")
        return
    except Exception as e:
        traceback.print_exc()
        _log_metrics("exception", time.time() - t0, None, job_id)
        try:
            ctx.reply_message(message_id, f"❌ 生图过程异常：{type(e).__name__}: {e}")
        except Exception:
            pass
    finally:
        if acquired:
            _GEN_SEM.release()
        if wf_path:
            try:
                Path(wf_path).unlink(missing_ok=True)
            except Exception:
                pass


def reply_image(message_id: str, image_path: str) -> bool:
    """上传图片并以 image 消息回复原会话（feishu_api 零改动，只读其运行时 client）。

    失败降级由调用方处理（发文本告知路径）。
    """
    try:
        import feishu_api  # ws_client 启动时 init_feishu_api 注入 client
        client = feishu_api.client
        if client is None:
            print("❌ reply_image: feishu_api.client 未初始化")
            return False

        from lark_oapi.api.im.v1 import (
            CreateImageRequest, CreateImageRequestBody,
            ReplyMessageRequest, ReplyMessageRequestBody)

        # 1. 上传图片拿 image_key
        with open(image_path, "rb") as f:
            upload_req = CreateImageRequest.builder() \
                .request_body(CreateImageRequestBody.builder()
                    .image_type("message")
                    .image(f)
                    .build()) \
                .build()
            upload_resp = client.im.v1.image.create(upload_req)
        if not upload_resp.success():
            print(f"❌ 图片上传失败: {upload_resp.code} - {upload_resp.msg}")
            return False
        # ⭐ 2026-08-09: 上传成功即记账(record_call 无枚举限制, 自由类型字符串)
        try:
            from feishu_quota import record_call
            record_call("image_upload")
        except Exception as e:
            print(f"⚠️ image_upload 配额记账异常(不阻断回图): {e}")
        image_key = upload_resp.data.image_key
        if not image_key:
            print("❌ 图片上传成功但未返回 image_key")
            return False

        # 2. image 消息回复原会话
        reply_req = ReplyMessageRequest.builder() \
            .message_id(message_id) \
            .request_body(ReplyMessageRequestBody.builder()
                .content(json.dumps({"image_key": image_key}))
                .msg_type("image")
                .build()) \
            .build()
        reply_resp = client.im.v1.message.reply(reply_req)
        if reply_resp.success():
            print(f"✅ 生图回复成功: {image_key}")
            from feishu_quota import record_call
            record_call("reply")
            return True
        print(f"❌ 图片回复失败: {reply_resp.code} - {reply_resp.msg}")
        return False
    except Exception as e:
        print(f"❌ reply_image 异常: {e}")
        return False
