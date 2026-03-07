"""
飞书通知发送器
检查任务通知并发送到飞书
"""

import json
import os
import time
from pathlib import Path


class NotificationSender:
    MAX_SEND_RETRIES = 3  # v32.4: 最大重试次数

    def __init__(self, feishu_api_module):
        """
        :param feishu_api_module: 已初始化的 feishu_api 模块
        """
        self.feishu_api = feishu_api_module
        self.notifications_dir = Path(__file__).parent.parent / "zhiwei-dev" / "notifications"
        self.user_mappings_dir = Path(__file__).parent.parent / "zhiwei-dev" / "user_mappings"

    def send_pending_notifications(self):
        """发送待处理的通知"""
        if not self.notifications_dir.exists():
            return

        # 获取所有通知文件
        for notify_file in self.notifications_dir.glob("task_*_notification.json"):
            try:
                with open(notify_file, 'r') as f:
                    notification = json.load(f)

                if not notification.get("sent", False):
                    task_id = notification["task_id"]

                    # v32.4: 检查重试次数
                    retry_count = notification.get("retry_count", 0)
                    if retry_count >= self.MAX_SEND_RETRIES:
                        # 超过重试上限，标记为已发送（失败）并停止重试
                        notification["sent"] = True
                        notification["send_error"] = f"超过最大重试次数 ({self.MAX_SEND_RETRIES})"
                        with open(notify_file, 'w') as f:
                            json.dump(notification, f, ensure_ascii=False, indent=2)
                        print(f"⚠️ 任务 {task_id} 通知放弃发送：已重试 {retry_count} 次")
                        continue

                    # 查找对应的用户ID
                    user_file = self.user_mappings_dir / f"task_{task_id}_user.json"
                    if user_file.exists():
                        with open(user_file, 'r') as uf:
                            user_data = json.load(uf)

                        user_id = user_data["user_id"]

                        # 发送通知
                        success = self.feishu_api.send_direct_message(user_id, notification["content"])

                        if success:
                            # 标记为已发送
                            notification["sent"] = True
                            with open(notify_file, 'w') as f:
                                json.dump(notification, f, ensure_ascii=False, indent=2)

                            print(f"✅ 任务 {task_id} 的通知已发送给用户 {user_id[:8]}...")

                            # 清理用户映射文件
                            try:
                                user_file.unlink()
                            except:
                                pass
                        else:
                            # v32.4: 递增重试计数
                            notification["retry_count"] = retry_count + 1
                            with open(notify_file, 'w') as f:
                                json.dump(notification, f, ensure_ascii=False, indent=2)

            except Exception as e:
                print(f"❌ 发送通知时出错 {notify_file}: {e}")

    def cleanup_old_notifications(self):
        """清理旧的通知文件"""
        if not self.notifications_dir.exists():
            return

        # 清理超过24小时的通知文件
        cutoff_time = time.time() - 24 * 3600  # 24小时前

        for notify_file in self.notifications_dir.glob("task_*_notification.json"):
            try:
                if notify_file.stat().st_mtime < cutoff_time:
                    notify_file.unlink()
                    print(f"🧹 清理旧通知文件: {notify_file.name}")
            except Exception as e:
                print(f"❌ 清理通知文件时出错 {notify_file}: {e}")


# 定时检查通知的守护进程
def run_notification_service(feishu_api_module, interval=5):
    """
    启动通知服务，定期检查并发送通知
    :param feishu_api_module: 已初始化的 feishu_api 模块
    :param interval: 检查间隔（秒）
    """
    sender = NotificationSender(feishu_api_module)

    print("🔔 通知服务启动")
    try:
        while True:
            sender.send_pending_notifications()
            sender.cleanup_old_notifications()
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n🔔 通知服务已停止")
    except Exception as e:
        print(f"❌ 通知服务出错: {e}")
        raise