#!/usr/bin/env python3
"""
Mimo 模型健康监控模块

监控 mimo-v2.5-tts、mimo-v2.5-asr、mimo-v2.5-pro 三个模型的健康状态，
实现连续失败计数、告警发送、冷却期控制。

状态文件: ~/logs/mimo_health.json
"""

import os
import sys
import json
import time
import logging
import base64
from pathlib import Path
from typing import Optional, Dict
from dataclasses import dataclass, field, asdict

import requests

logger = logging.getLogger(__name__)

# ==================== 配置常量 ====================

MIMO_API_BASE = os.getenv("MIMO_API_BASE", "https://token-plan-cn.xiaomimimo.com")
ALERT_COOLDOWN_SECONDS = 3600  # 1 小时冷却期
ALERT_THRESHOLD = 3  # 连续失败 3 次触发告警
HEALTH_STATE_FILE = Path.home() / "logs" / "mimo_health.json"

# 监控的模型
MONITORED_MODELS = {
    "mimo-v2.5-tts": {"timeout": 15, "type": "tts"},
    "mimo-v2.5-asr": {"timeout": 15, "type": "asr"},
    "mimo-v2.5-pro": {"timeout": 15, "type": "chat"},
}


@dataclass
class ModelHealth:
    """单个模型的健康状态"""
    name: str
    healthy: bool = True
    last_check_time: str = ""
    last_success_time: str = ""
    last_error: str = ""
    consecutive_fails: int = 0
    total_checks: int = 0
    total_success: int = 0
    total_fails: int = 0
    last_alert_time: int = 0


class MimoMonitor:
    """Mimo 模型健康监控"""

    def __init__(self, api_key: Optional[str] = None, alert_user_id: Optional[str] = None):
        self.api_key = api_key or os.getenv("MIMO_API_KEY", "")
        self.alert_user_id = alert_user_id or os.getenv("ALERT_USER_ID", "")
        self.health_state: Dict[str, ModelHealth] = {}
        self._initialized = False

        # 加载历史状态
        self._load_state()

        # 初始化监控项
        for model_name, config in MONITORED_MODELS.items():
            if model_name not in self.health_state:
                self.health_state[model_name] = ModelHealth(name=model_name)

        self._initialized = True
        logger.info(f"✅ MimoMonitor 已初始化，监控 {len(MONITORED_MODELS)} 个模型")

    def _load_state(self):
        """从文件加载历史状态"""
        try:
            if HEALTH_STATE_FILE.exists():
                data = json.loads(HEALTH_STATE_FILE.read_text())
                for model_name, state_data in data.items():
                    self.health_state[model_name] = ModelHealth(**state_data)
                logger.info(f"[MimoMonitor] 加载历史状态: {len(self.health_state)} 个模型")
        except Exception as e:
            logger.warning(f"[MimoMonitor] 加载状态失败: {e}")

    def _save_state(self):
        """保存状态到文件"""
        try:
            HEALTH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = {
                name: asdict(health)
                for name, health in self.health_state.items()
            }
            HEALTH_STATE_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        except Exception as e:
            logger.warning(f"[MimoMonitor] 保存状态失败: {e}")

    def _now_iso(self) -> str:
        """获取当前 ISO 时间字符串"""
        from datetime import datetime
        return datetime.now().isoformat()

    def check_health(self, model_name: Optional[str] = None) -> Dict[str, bool]:
        """执行健康检查

        Args:
            model_name: 指定模型名，为 None 时检查所有模型

        Returns:
            {model_name: is_healthy} 字典
        """
        results = {}
        models_to_check = {model_name: MONITORED_MODELS[model_name]} if model_name else MONITORED_MODELS

        for name, config in models_to_check.items():
            if name not in self.health_state:
                continue

            health = self.health_state[name]
            health.total_checks += 1
            health.last_check_time = self._now_iso()

            try:
                is_healthy = self._check_single_model(name, config)
                health.healthy = is_healthy
                results[name] = is_healthy

                if is_healthy:
                    health.consecutive_fails = 0
                    health.last_success_time = self._now_iso()
                    health.total_success += 1
                    logger.info(f"[MimoMonitor] ✅ {name} 健康")
                else:
                    health.consecutive_fails += 1
                    health.total_fails += 1
                    logger.warning(f"[MimoMonitor] ❌ {name} 不健康 (连续失败: {health.consecutive_fails})")

                self._save_state()

            except Exception as e:
                health.healthy = False
                health.consecutive_fails += 1
                health.total_fails += 1
                health.last_error = str(e)[:200]
                results[name] = False
                logger.error(f"[MimoMonitor] ❌ {name} 异常: {e}")
                self._save_state()

        return results

    def _check_single_model(self, model_name: str, config: dict) -> bool:
        """检查单个模型是否可用

        Returns:
            True = 健康, False = 不健康
        """
        if not self.api_key:
            return False

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{MIMO_API_BASE}/v1/chat/completions"

        model_type = config["type"]
        timeout = config["timeout"]

        if model_type == "tts":
            payload = {
                "model": model_name,
                "max_tokens": 100,
                "messages": [{"role": "assistant", "content": "测试"}]
            }
            r = requests.post(url, headers=headers, json=payload, timeout=timeout)
            if r.status_code != 200:
                return False
            d = r.json()
            audio = d.get("choices", [{}])[0].get("message", {}).get("audio")
            return audio is not None and audio.get("data") is not None

        elif model_type == "asr":
            # ASR 需要先 TTS 生成测试音频
            tts_payload = {
                "model": "mimo-v2.5-tts",
                "max_tokens": 1024,
                "messages": [{"role": "assistant", "content": "测试"}]
            }
            r = requests.post(url, headers=headers, json=tts_payload, timeout=10)
            if r.status_code != 200:
                return False
            d = r.json()
            tts_data = d.get("choices", [{}])[0].get("message", {}).get("audio", {}).get("data")
            if not tts_data:
                return False

            # 转录
            b64 = base64.b64encode(base64.b64decode(tts_data)).decode()
            data_url = f"data:audio/wav;base64,{b64}"
            asr_payload = {
                "model": model_name,
                "max_tokens": 50,
                "messages": [{"role": "user", "content": [
                    {"type": "input_audio", "input_audio": {"data": data_url}}
                ]}]
            }
            r2 = requests.post(url, headers=headers, json=asr_payload, timeout=10)
            if r2.status_code != 200:
                return False
            d2 = r2.json()
            text = d2.get("choices", [{}])[0].get("message", {}).get("content", "")
            return len(text) > 0

        elif model_type == "chat":
            payload = {
                "model": model_name,
                "max_tokens": 50,
                "messages": [{"role": "user", "content": "回复一个字"}]
            }
            r = requests.post(url, headers=headers, json=payload, timeout=timeout)
            if r.status_code != 200:
                return False
            d = r.json()
            content = d.get("choices", [{}])[0].get("message", {}).get("content", "")
            return len(content) > 0

        return False

    def check_and_alert(self) -> list:
        """执行健康检查并发送告警

        Returns:
            发送的告警列表 [(model_name, alert_message), ...]
        """
        alerts = []
        results = self.check_health()

        for model_name, is_healthy in results.items():
            health = self.health_state.get(model_name)
            if not health:
                continue

            if not is_healthy and health.consecutive_fails >= ALERT_THRESHOLD:
                # 检查冷却期
                now = int(time.time())
                if now - health.last_alert_time < ALERT_COOLDOWN_SECONDS:
                    logger.info(f"[MimoMonitor] {model_name} 连续失败，但处于冷却期，跳过告警")
                    continue

                # 发送告警
                msg = f"⚠️ Mimo 模型异常\n\n模型: {model_name}\n连续失败: {health.consecutive_fails} 次\n最后错误: {health.last_error[:100]}\n\n已自动降级到其他服务。"
                if self._send_alert(msg):
                    health.last_alert_time = now
                    self._save_state()
                    alerts.append((model_name, msg))

            elif is_healthy and health.last_alert_time > 0:
                # 检查是否刚恢复（上次告警后的首次恢复）
                now = int(time.time())
                if now - health.last_alert_time < ALERT_COOLDOWN_SECONDS:
                    msg = f"✅ Mimo 模型已恢复\n\n模型: {model_name}\n已连续成功\n\n所有服务恢复正常。"
                    if self._send_alert(msg):
                        alerts.append((model_name, msg))

        return alerts

    def _send_alert(self, message: str) -> bool:
        """发送告警到飞书

        Args:
            message: 告警消息

        Returns:
            是否发送成功
        """
        if not self.alert_user_id:
            logger.warning("[MimoMonitor] ALERT_USER_ID 未配置，跳过告警")
            return False

        try:
            # 复用 send_direct_message
            sys.path.insert(0, str(Path(__file__).parent))
            try:
                from feishu_api import send_direct_message
                return send_direct_message(self.alert_user_id, message)
            except ImportError:
                logger.warning("[MimoMonitor] feishu_api 未初始化")
                return False
        except Exception as e:
            logger.error(f"[MimoMonitor] 发送告警异常: {e}")
            return False

    def get_status(self) -> Dict:
        """获取当前健康状态（用于查询接口）"""
        status = {}
        for name, health in self.health_state.items():
            status[name] = {
                "healthy": health.healthy,
                "consecutive_fails": health.consecutive_fails,
                "last_check": health.last_check_time,
                "last_success": health.last_success_time,
                "last_error": health.last_error,
                "total_checks": health.total_checks,
                "total_success": health.total_success,
                "total_fails": health.total_fails,
            }
        return status


# ==================== 便捷函数 ====================

def run_health_check(alert_user_id: Optional[str] = None) -> Dict:
    """执行一次健康检查并告警（供 scheduler 调用）

    Returns:
        健康状态字典
    """
    monitor = MimoMonitor(alert_user_id=alert_user_id)
    alerts = monitor.check_and_alert()
    status = monitor.get_status()

    # 打印摘要
    all_healthy = all(s["healthy"] for s in status.values())
    if all_healthy:
        logger.info("✅ Mimo 所有模型健康检查通过")
    else:
        logger.warning(f"⚠️ Mimo 有 {sum(1 for s in status.values() if not s['healthy'])} 个模型异常")

    if alerts:
        logger.info(f"📢 发送了 {len(alerts)} 条告警")

    return status


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    alert_user = os.getenv("ALERT_USER_ID")
    result = run_health_check(alert_user_id=alert_user)
    print(json.dumps(result, indent=2, ensure_ascii=False))
