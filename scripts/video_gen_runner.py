#!/usr/bin/env python3
"""H3 视频生成 runner（detached 执行，2026-08-09）

由 video_gen.action_confirm 以 start_new_session 方式拉起，脱离 bot 进程存活。
流程：gpu_offload.submit("h3", sync) → 成功则 mp4 经飞书文件消息直传
（2026-08-11 OSS 退订后替代 OSS 签名链接；≤30MB 直传，超限/失败走兜底话术）；
失败推送原因。无论成败都清理 pending 状态。

用法: python3 video_gen_runner.py <payload.json>
payload: {user_id, prompt, duration, width, height, steps, request}
"""
import json
import os
import sys
import time
from pathlib import Path

from zhiwei_common.secrets import load_secrets
load_secrets(silent=True)

sys.path.insert(0, str(Path.home() / "zhiwei-bot"))

SUBMIT_TIMEOUT = 3000  # h3 worker 上限 3600，留余量
FEISHU_FILE_LIMIT = 30 * 1024 * 1024  # 飞书 im/v1/files 单文件上限约 30MB
STATE_FILE = Path.home() / "zhiwei-bot" / "state" / "video_gen_pending.json"


def _clear_pending(user_id: str):
    try:
        if STATE_FILE.exists():
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if user_id in state:
                state.pop(user_id, None)
                STATE_FILE.write_text(json.dumps(state, ensure_ascii=False),
                                      encoding="utf-8")
    except Exception as e:
        print(f"⚠️ pending 清理失败: {e}")


def _pusher(prefer: str = "bot"):
    """构造 FeishuPusher。

    prefer="bot"：主凭证 FEISHU_APP_*（知微应用）。
    prefer="scheduler"：探微凭证 SCHEDULER_FEISHU_*（知微应用未开通
    im:resource 文件上传权限，2026-08-11 实测；chat 回退 FEISHU_CHAT_ID）。
    """
    from zhiwei_common.pusher import FeishuPusher
    chat_id = os.environ.get("FEISHU_CHAT_ID", "")
    if prefer == "scheduler":
        app_id = os.environ.get("SCHEDULER_FEISHU_APP_ID", "")
        app_secret = os.environ.get("SCHEDULER_FEISHU_APP_SECRET", "")
        chat_id = os.environ.get("SCHEDULER_FEISHU_CHAT_ID", "") or chat_id
    else:
        app_id = os.environ.get("FEISHU_APP_ID", "")
        app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    if not (app_id and app_secret and chat_id):
        return None
    return FeishuPusher(app_id, app_secret, chat_id)


def _notify(title: str, content: str):
    pusher = _pusher()
    if not pusher:
        print("❌ 飞书凭证不全，无法推送结果")
        return False
    try:
        r = pusher.send_markdown(title, content)
        return r.get("code") == 0
    except Exception as e:
        print(f"❌ 结果推送异常: {e}")
        return False


def main():
    payload_path = sys.argv[1]
    payload = json.loads(Path(payload_path).read_text(encoding="utf-8"))
    user_id = payload["user_id"]
    duration = int(payload["duration"])
    t0 = time.time()

    from zhiwei_common import gpu_offload
    result = gpu_offload.submit(
        "h3", inputs=[],
        params={
            "prompt": payload["prompt"],
            "width": int(payload["width"]),
            "height": int(payload["height"]),
            "length": duration * 24,
            "steps": int(payload.get("steps", 20)),
            "timeout": SUBMIT_TIMEOUT,
        },
        mode="sync", timeout=SUBMIT_TIMEOUT + 300)

    elapsed = round((time.time() - t0) / 60, 1)

    if not result or result.get("status") != "done":
        reason = "GPU 工作站不在线或任务失败"
        if isinstance(result, dict) and result.get("error"):
            reason = str(result["error"])[:200]
        _clear_pending(user_id)
        _notify("❌ H3 视频生成失败",
                f"**原因**：{reason}\n\n"
                f"需求：{payload.get('request', '')[:100]}\n"
                f"（GPU 恢复后可重新发起；不会自动转 API，避免未确认的花费）")
        print(f"❌ H3 生成失败: {reason}")
        return 1

    # 取回 mp4 → 飞书文件消息直传（2026-08-11 替代 OSS 签名链接）
    try:
        meta = result.get("outputs_meta", {})
        video_name = meta.get("video")
        local_mp4 = f"/tmp/h3_result_{int(time.time())}.mp4"
        gpu_offload.fetch_output(result, f"out/{video_name}", local_mp4)
    except Exception as e:
        _clear_pending(user_id)
        _notify("⚠️ H3 视频已生成但交付失败",
                f"取回环节出错：{e}\n视频仍在笔记本 ComfyUI/output/，可手动取回。")
        print(f"❌ 交付失败: {e}")
        return 1

    specs = (f"{payload['width']}×{payload['height']} / {duration} 秒 / 含原生音频")
    delivered = False
    deliver_reason = ""
    try:
        size = os.path.getsize(local_mp4)
        if size > FEISHU_FILE_LIMIT:
            deliver_reason = (f"视频 {size/1024/1024:.1f}MB 超过飞书单文件 30MB 上限")
        else:
            # 主凭证（知微应用）→ 失败回退探微凭证（im:resource 权限差异，2026-08-11 实测）
            for prefer in ("bot", "scheduler"):
                pusher = _pusher(prefer)
                if pusher is None:
                    continue
                file_key = pusher.upload_file(
                    local_mp4,
                    file_name=f"h3_video_{int(time.time())}.mp4")
                if file_key and pusher.send_file(file_key).get("code") == 0:
                    delivered = True
                    break
            if not delivered:
                deliver_reason = "飞书文件上传/发送失败（两套凭证均失败或凭证不全）"
    except Exception as e:
        deliver_reason = str(e)[:200]
    finally:
        try:
            os.remove(local_mp4)
        except OSError:
            pass

    _clear_pending(user_id)
    if not delivered:
        _notify("⚠️ H3 视频已生成但交付降级",
                f"**原因**：{deliver_reason}\n"
                f"**规格**：{specs}\n**总耗时**：{elapsed} 分钟\n\n"
                f"视频在笔记本 ComfyUI/output/，可手动取回。")
        print(f"⚠️ 交付降级: {deliver_reason}")
        return 0

    _notify("🎬 H3 视频生成完成",
            f"**规格**：{specs}\n"
            f"**总耗时**：{elapsed} 分钟\n\n"
            f"视频已作为飞书文件消息发送（见上一条），可直接下载保存。\n\n"
            f"需求回顾：{payload.get('request', '')[:100]}")
    print(f"✅ H3 生成完成 {elapsed}min（飞书直传）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
