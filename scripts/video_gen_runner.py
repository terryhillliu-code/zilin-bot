#!/usr/bin/env python3
"""H3 视频生成 runner（detached 执行，2026-08-09）

由 video_gen.action_confirm 以 start_new_session 方式拉起，脱离 bot 进程存活。
流程：gpu_offload.submit("h3", sync) → 成功则上传 OSS 出 7 天直链 →
FeishuPusher 推送结果；失败推送原因。无论成败都清理 pending 状态。

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


def _notify(title: str, content: str):
    from zhiwei_common.pusher import FeishuPusher
    app_id = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    chat_id = os.environ.get("FEISHU_CHAT_ID", "")
    if not (app_id and app_secret and chat_id):
        print("❌ 飞书凭证不全，无法推送结果")
        return False
    try:
        r = FeishuPusher(app_id, app_secret, chat_id).send_markdown(title, content)
        return r.get("code") == 0
    except Exception as e:
        print(f"❌ 结果推送失败: {e}")
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

    # 取回 mp4 → 上传 OSS → 7 天直链
    try:
        meta = result.get("outputs_meta", {})
        video_name = meta.get("video")
        local_mp4 = f"/tmp/h3_result_{int(time.time())}.mp4"
        gpu_offload.fetch_output(result, f"out/{video_name}", local_mp4)

        import oss2
        cfg = {}
        cred = Path.home() / ".config/zhiwei-oss/credentials"
        for line in cred.read_text().splitlines():
            if "=" in line:
                k, v = line.strip().split("=", 1)
                cfg[k] = v
        bucket = oss2.Bucket(
            oss2.Auth(cfg["OSS_ACCESS_KEY_ID"], cfg["OSS_ACCESS_KEY_SECRET"]),
            cfg["OSS_ENDPOINT"], cfg["OSS_BUCKET"])
        key = f"tmp/h3_feishu_{int(time.time())}.mp4"
        bucket.put_object_from_file(key, local_mp4)
        url = bucket.sign_url("GET", key, 7 * 86400, slash_safe=True)
        os.remove(local_mp4)
    except Exception as e:
        _clear_pending(user_id)
        _notify("⚠️ H3 视频已生成但交付失败",
                f"取回/上传环节出错：{e}\n视频仍在笔记本 ComfyUI/output/，可手动取回。")
        print(f"❌ 交付失败: {e}")
        return 1

    _clear_pending(user_id)
    _notify("🎬 H3 视频生成完成",
            f"**规格**：{payload['width']}×{payload['height']} / {duration} 秒 / 含原生音频\n"
            f"**总耗时**：{elapsed} 分钟\n\n"
            f"[▶ 点击播放/下载（7 天有效）]({url})\n\n"
            f"需求回顾：{payload.get('request', '')[:100]}")
    print(f"✅ H3 生成完成 {elapsed}min")
    return 0


if __name__ == "__main__":
    sys.exit(main())
