"""WebSocket 心跳监控与连接监控（2026-07-30 从 ws_client.py 拆分）

职责：
- 心跳文件写入 (~/logs/ws_heartbeat.json)，供 watchdog 检测
- 消息事件计数（区分"连接活着"和"真正在接收消息"）
- 连接监控线程：僵尸连接检测、离线恢复触发、memory_cache 定期清理
- WebSocket 断连告警（钉钉，含频率控制）

依赖注入：get_offline_recovery / load_active_user / cleanup_memory_cache
由 ws_client 调用 start_connection_monitor 时传入，避免循环 import。
"""

import json
import os
import threading
import time
from datetime import datetime

# ========== WebSocket 心跳监控 (v44.4) ==========
HEARTBEAT_FILE = os.path.expanduser("~/logs/ws_heartbeat.json")

# 连接状态监控 (ISSUE-003 / ISSUE-027 修复)
# 简化版：仅监控业务事件，避免误判
connection_status = {
    "connected": True,
    "last_event": time.time(),       # 业务事件（收到消息）
}

# 消息事件计数器 (2026-06-02 加固)
# 用于 watchdog 区分"连接活着"和"真正在接收消息"
message_event_count = 0


def record_message_event():
    """递增消息事件计数器（2026-06-02 加固）"""
    global message_event_count
    message_event_count += 1


def write_heartbeat(conn_id: str = "", status: str = "connected"):
    """写入心跳状态文件，供 watchdog 检测

    2026-06-02 增强：加入消息事件计数，让 watchdog 能区分
    "连接活着但 message loop 已死" 和 "连接正常工作" 的状态。
    """
    try:
        with open(HEARTBEAT_FILE, "w") as f:
            json.dump({
                "timestamp": time.time(),
                "conn_id": conn_id,
                "status": status,
                "message_events": message_event_count  # ⭐ 新增
            }, f)
    except Exception:
        pass  # 心跳写入失败不影响主流程


# ISSUE-003: 断连监控和告警

# 告警状态文件路径
ALERT_STATE_FILE = os.path.expanduser("~/logs/ws_alert_state.json")


def load_alert_state() -> dict:
    """加载告警状态"""
    try:
        if os.path.exists(ALERT_STATE_FILE):
            with open(ALERT_STATE_FILE) as f:
                return json.load(f)
    except (json.JSONDecodeError, IOError):
        pass  # 告警状态加载失败，使用默认值
    return {"last_alert_time": 0, "alert_type": None}


def save_alert_state(state: dict):
    """保存告警状态"""
    try:
        with open(ALERT_STATE_FILE, "w") as f:
            json.dump(state, f)
    except IOError:
        pass  # 告警状态保存失败不影响主流程


def send_ws_alert(msg: str, alert_type: str = "disconnect") -> bool:
    """发送 WebSocket 告警（通过钉钉，避免消耗飞书额度）"""
    state = load_alert_state()
    now = time.time()

    # 告警频率控制：同一类型告警每小时最多发一次
    if state.get("alert_type") == alert_type:
        if now - state.get("last_alert_time", 0) < 3600:
            print(f"⏭️ 告警频率限制，跳过推送：{alert_type}")
            return False

    # 尝试通过钉钉发送
    try:
        from zhiwei_common import DingTalkPusher
        import yaml

        config_path = os.path.expanduser("~/zhiwei-scheduler/config/settings.yaml")
        with open(config_path) as f:
            dt_conf = yaml.safe_load(f).get("push", {}).get("dingtalk", {})

        if dt_conf.get("enabled"):
            pusher = DingTalkPusher(dt_conf["webhook"], dt_conf["secret"])
            pusher.send_text(msg)
            print(f"📱 WebSocket 告警已发送: {msg[:50]}...")

            # 更新告警状态
            save_alert_state({
                "last_alert_time": now,
                "alert_type": alert_type
            })
            return True
        else:
            print("⚠️ 钉钉未启用，无法发送告警")
    except Exception as e:
        print(f"❌ 发送 WebSocket 告警失败: {e}")

    return False


def start_connection_monitor(get_offline_recovery, load_active_user, cleanup_memory_cache):
    """启动连接监控线程（依赖注入，避免循环 import），返回线程对象"""

    def connection_monitor():
        """连接监控线程 - 优化版 (v44.5, 2026-06-02 加固)

        功能：
        1. 每分钟写入心跳文件（供 watchdog 检测）
        2. 业务消息空闲时记录日志（不发送钉钉告警，避免误报）
        3. ⭐ 离线恢复检测：长时间空闲后恢复时尝试恢复离线消息
        4. ⭐ v48.0: 定期清理 memory_cache（每10分钟）
        5. ⭐ 2026-06-02: 僵尸连接检测（心跳正常但不收消息时主动重连）
        """
        # 启动时立即写入心跳
        write_heartbeat(status="starting")

        # 离线检测状态
        was_idle_long = False  # 上一次检查时是否长时间空闲
        cleanup_counter = 0  # 清理计数器
        catchup_counter = 0  # ⭐ 2026-08-05: 兜底补跑计数器
        last_event_count = message_event_count  # 上次检查时的事件计数
        zombie_idle_start = None  # 僵尸连接开始时间
        last_check_time = time.time()  # ⭐ 2026-08-05: 唤醒检测基准

        def _has_active_distill() -> bool:
            """是否有在跑的蒸馏子进程（视频/PDF/音频三类，杀掉会丢用户任务）"""
            try:
                import subprocess as _sp
                return _sp.run(
                    ["pgrep", "-f", "douyin_distiller|pdf_distiller|audio_distiller"],
                    capture_output=True, timeout=5).returncode == 0
            except Exception:
                return False

        while True:
            time.sleep(60)  # 每分钟检查一次
            now = time.time()

            # ⭐ 2026-08-05: 唤醒检测——循环间隔应约 60s，超过 180s 说明机器刚休眠醒来。
            # 休眠必然杀死 WebSocket（半开连接），SDK keepalive 无法感知，
            # 直接强制重启重建连接（launchd KeepAlive 拉起，兜底补跑补漏消息）。
            # （2026-08-05 两起事故实证：04:11 与 05:10 消息均因休眠后僵尸连接丢失）
            if now - last_check_time > 180:
                gap_min = int((now - last_check_time) / 60)
                if _has_active_distill():
                    print(f"⏰ 检测到系统唤醒（间隔 {gap_min} 分钟），但有活跃蒸馏任务，推迟重启")
                else:
                    print(f"⏰ 检测到系统唤醒（监控间隔 {gap_min} 分钟），强制重启重建飞书连接...")
                    with open(os.path.expanduser("~/logs/connection_monitor.log"), "a") as f:
                        f.write(f"{datetime.now().isoformat()}: WAKE detected (gap {gap_min}min), forcing restart\n")
                    os._exit(75)
            last_check_time = now

            event_idle = now - connection_status.get("last_event", now)

            # ⭐ 僵尸连接检测：如果事件计数不增长
            current_count = message_event_count
            if current_count == last_event_count:
                if zombie_idle_start is None:
                    zombie_idle_start = now  # 开始计时
            else:
                zombie_idle_start = None  # 有消息来了，重置
                last_event_count = current_count

            # 连续 2 小时没收到消息——记录但不再重启（H1修复: 原版误杀健康空闲）
            ZOMBIE_THRESHOLD = 7200  # 2 小时
            if zombie_idle_start and (now - zombie_idle_start) > ZOMBIE_THRESHOLD:
                zombie_minutes = int((now - zombie_idle_start) / 60)
                print(f"🚨 僵尸连接检测：{zombie_minutes} 分钟未收到任何消息，主动断开重连...")
                zombie_idle_start = now  # H1修复: 重置计时，每2h记一次不刷屏

                # 记录到日志
                with open(os.path.expanduser("~/logs/connection_monitor.log"), "a") as f:
                    f.write(f"{datetime.now().isoformat()}: ZOMBIE detected, forcing reconnect after {zombie_minutes}min\n")

                # ⭐ 2026-08-05: 恢复强制重启——2026-08-05 04:11 事故实证 SDK keepalive
                # 无法捕获机器休眠导致的半开连接（asyncio 事件循环静默挂起 3.5 小时，
                # 用户消息全部丢失）。H1 担心的"误杀健康空闲"实际无害：空闲期重启只是
                # 重连一次。唯一风险是打断在跑的蒸馏，故先检查活跃任务，有则推迟。
                _active = _has_active_distill()
                if _active:
                    print("⚠️ 有活跃蒸馏任务，推迟僵尸重启（下个周期再检查）")
                else:
                    print("🚨 无活跃任务，强制重启进程（launchd KeepAlive 自动拉起，离线恢复补漏消息）")
                    with open(os.path.expanduser("~/logs/connection_monitor.log"), "a") as f:
                        f.write(f"{datetime.now().isoformat()}: ZOMBIE restart executed (no active tasks)\n")
                    os._exit(75)

            # ⭐ 2026-08-05: 兜底补跑——每 5 分钟扫描 message_log 中已接收但未处理的消息
            # （2026-08-05 04:11 事故：机器休眠竞态导致消息已收未处理，用户自然语言石沉大海。
            #  mark_processed 由 command_handler 在处理开始时打标，此处补跑漏网消息）
            catchup_counter += 1
            if catchup_counter >= 5:
                catchup_counter = 0
                try:
                    from message_log import message_log as _msg_log
                    pending = _msg_log.get_unprocessed(min_age_seconds=120, hours_limit=6)
                    if pending:
                        print(f"🔁 兜底补跑：发现 {len(pending)} 条未处理消息")
                        from command_handler import handle_text_async
                        for m in pending:
                            print(f"   📨 补跑: {m['received_at']} {str(m['content'])[:40]}...")
                            threading.Thread(
                                target=handle_text_async,
                                args=(m["content"], m["user_id"], m["message_id"]),
                                daemon=True
                            ).start()
                            time.sleep(2)  # 串行间隔，避免并发冲击
                except Exception as e:
                    print(f"⚠️ 兜底补跑异常: {e}")

            # 写入心跳（即使空闲也写入，表示服务存活）
            write_heartbeat(status="connected")

            # ⭐ v48.0: 每10分钟清理 memory_cache
            cleanup_counter += 1
            if cleanup_counter >= 10:
                cleanup_counter = 0
                try:
                    cleanup_memory_cache()
                except Exception as e:
                    print(f"⚠️ memory_cache 清理异常: {e}")

            # 检测长时间空闲（超过 5 分钟）
            is_idle_long = event_idle > 300  # 5 分钟

            # ⭐ 离线恢复检测：从长时间空闲恢复到活跃
            if was_idle_long and not is_idle_long:
                # 刚从长时间空闲恢复，尝试离线恢复
                offline_recovery = get_offline_recovery()
                if offline_recovery and offline_recovery.should_recover(threshold_seconds=300):
                    idle_minutes = int(event_idle / 60)
                    print(f"🔄 检测到离线恢复（空闲 {idle_minutes} 分钟），尝试恢复离线消息...")

                    # 获取最近活跃用户
                    active_user = load_active_user()
                    if active_user:
                        try:
                            # 获取私聊会话 ID
                            chat_id = offline_recovery.get_p2p_chat_id(active_user)
                            if chat_id:
                                # 恢复离线消息
                                since_time = offline_recovery.state.get("last_disconnect_time", time.time() - 3600)
                                messages = offline_recovery.recover_messages(chat_id, since_time)
                                if messages:
                                    print(f"📬 恢复了 {len(messages)} 条离线消息")
                                    # 处理恢复的消息（模拟消息事件）
                                    for msg in messages[-5:]:  # 最多处理最近 5 条
                                        print(f"   📨 离线消息: {msg.content[:50] if msg.content else 'N/A'}...")
                        except Exception as e:
                            print(f"⚠️ 离线恢复失败: {e}")

                    # 记录重连时间
                    offline_recovery.record_reconnect()

            # 长时间空闲时记录断连时间（必须在 was_idle_long 更新之前检查）
            if is_idle_long and not was_idle_long:
                offline_recovery = get_offline_recovery()
                if offline_recovery:
                    offline_recovery.record_disconnect()

            # 更新空闲状态
            was_idle_long = is_idle_long

            # 业务消息空闲超过 30 分钟才记录日志（不再发送钉钉告警）
            if event_idle > 1800:  # 30 分钟
                idle_minutes = int(event_idle / 60)

                # 仅记录到日志，不发送钉钉告警
                with open(os.path.expanduser("~/logs/connection_monitor.log"), "a") as f:
                    f.write(f"{datetime.now().isoformat()}: Business idle {idle_minutes}min (normal)\n")

                print(f"💡 业务消息空闲 {idle_minutes} 分钟（正常现象，连接通过 ping 保持）")

                # 重置时间戳，避免频繁记录日志
                connection_status["last_event"] = now

    # 启动监控线程
    monitor_thread = threading.Thread(target=connection_monitor, daemon=True)
    monitor_thread.start()
    return monitor_thread
