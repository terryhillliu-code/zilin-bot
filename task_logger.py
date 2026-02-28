"""任务日志 - 持久化记录已完成的任务"""
import os
from datetime import datetime


class TaskLogger:
    LOG_FILE = os.path.expanduser("~/logs/tasks_done.md")

    @classmethod
    def log_task(cls, task_name, result, details=""):
        os.makedirs(os.path.dirname(cls.LOG_FILE), exist_ok=True)
        with open(cls.LOG_FILE, 'a') as f:
            f.write(f"\n## {datetime.now().strftime('%Y-%m-%d %H:%M')} - {task_name}\n")
            f.write(f"- 结果: {result}\n")
            if details:
                f.write(f"- 详情: {details}\n")

    @classmethod
    def get_recent(cls, n=10) -> str:
        if not os.path.exists(cls.LOG_FILE):
            return "📋 暂无任务记录"
        with open(cls.LOG_FILE, 'r') as f:
            content = f.read()
        sections = content.split('\n## ')
        if len(sections) <= 1:
            return "📋 暂无任务记录"
        recent = sections[-n:]
        result = "📋 最近任务记录\n\n"
        for section in recent:
            section = section.strip()
            if section:
                result += f"## {section}\n\n"
        return result

    @classmethod
    def search(cls, keyword) -> str:
        if not os.path.exists(cls.LOG_FILE):
            return "📋 暂无匹配记录"
        with open(cls.LOG_FILE, 'r') as f:
            content = f.read()
        sections = content.split('\n## ')
        matches = [s for s in sections[1:] if keyword.lower() in s.lower()]
        if not matches:
            return f"📋 未找到包含「{keyword}」的任务记录"
        result = f"📋 搜索「{keyword}」的结果\n\n"
        for section in matches[-5:]:
            result += f"## {section.strip()}\n\n"
        return result
