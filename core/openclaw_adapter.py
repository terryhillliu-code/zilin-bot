"""
OpenClaw 白名单调用适配器
- 只允许 browser 和 sandbox 两类能力
- 业务代码禁止直接 docker exec
- 支持 kill-switch
"""
import subprocess
import os
import logging
from typing import Optional, Tuple, Dict, Any

logger = logging.getLogger(__name__)


class OpenClawAdapter:
    """OpenClaw 可选执行舱适配器"""

    # 环境变量控制（kill-switch）
    ENABLED = os.getenv("OPENCLAW_ENABLED", "1") == "1"
    BROWSER_ENABLED = os.getenv("OPENCLAW_BROWSER_ENABLED", "1") == "1"
    SANDBOX_ENABLED = os.getenv("OPENCLAW_SANDBOX_ENABLED", "1") == "1"

    CONTAINER_NAME = "clawdbot"
    TIMEOUT = 60

    @classmethod
    def is_available(cls) -> bool:
        """检查 OpenClaw 容器是否运行"""
        if not cls.ENABLED:
            return False

        try:
            result = subprocess.run(
                ["docker", "ps", "-q", "-f", f"name={cls.CONTAINER_NAME}"],
                capture_output=True,
                text=True,
                timeout=5
            )
            return bool(result.stdout.strip())
        except Exception as e:
            logger.warning(f"检查容器状态失败: {e}")
            return False

    @classmethod
    def run_browser(
        cls,
        action: str,
        url: str = "",
        selector: str = "",
        text: str = ""
    ) -> Tuple[bool, str]:
        """
        运行浏览器任务

        Args:
            action: 动作类型
                - open: 打开 URL
                - screenshot: 截图
                - extract: 提取文本
                - click: 点击元素
                - type: 输入文本
            url: 目标 URL
            selector: CSS 选择器（click/type 用）
            text: 输入文本（type 用）

        Returns:
            (成功与否, 输出内容或错误信息)
        """
        if not cls.ENABLED:
            return False, "OpenClaw 已全局禁用"

        if not cls.BROWSER_ENABLED:
            return False, "Browser 功能已禁用"

        if not cls.is_available():
            return False, "OpenClaw 容器未运行"

        # 构建命令
        cmd = ["docker", "exec", cls.CONTAINER_NAME, "openclaw", "browser", action]

        if url:
            cmd.append(url)
        if selector:
            cmd.extend(["--selector", selector])
        if text:
            cmd.extend(["--text", text])

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=cls.TIMEOUT
            )

            if result.returncode == 0:
                return True, result.stdout
            else:
                return False, result.stderr or f"返回码: {result.returncode}"

        except subprocess.TimeoutExpired:
            logger.error(f"Browser 任务超时: {action}")
            return False, "任务超时"
        except Exception as e:
            logger.error(f"Browser 任务失败: {e}")
            return False, str(e)

    @classmethod
    def run_sandbox(
        cls,
        language: str,
        code: str,
        timeout: int = 30
    ) -> Tuple[bool, str]:
        """
        运行沙箱代码

        Args:
            language: 语言类型 (python/bash/javascript)
            code: 要执行的代码
            timeout: 超时时间（秒）

        Returns:
            (成功与否, 输出内容或错误信息)
        """
        if not cls.ENABLED:
            return False, "OpenClaw 已全局禁用"

        if not cls.SANDBOX_ENABLED:
            return False, "Sandbox 功能已禁用"

        if not cls.is_available():
            return False, "OpenClaw 容器未运行"

        # 语言白名单
        allowed_languages = ["python", "bash", "javascript"]
        if language not in allowed_languages:
            return False, f"不支持的语言: {language}，允许: {allowed_languages}"

        cmd = [
            "docker", "exec", cls.CONTAINER_NAME,
            "openclaw", "skill", "code-executor",
            "--language", language,
            "--code", code,
            "--timeout", str(timeout)
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout + 10  # 额外 10 秒缓冲
            )

            if result.returncode == 0:
                return True, result.stdout
            else:
                return False, result.stderr or f"返回码: {result.returncode}"

        except subprocess.TimeoutExpired:
            logger.error(f"Sandbox 任务超时: {language}")
            return False, "任务超时"
        except Exception as e:
            logger.error(f"Sandbox 任务失败: {e}")
            return False, str(e)

    @classmethod
    def health_check(cls) -> Dict[str, Any]:
        """
        健康检查

        Returns:
            {
                "enabled": bool,
                "available": bool,
                "browser_enabled": bool,
                "sandbox_enabled": bool,
                "container_status": str
            }
        """
        result = {
            "enabled": cls.ENABLED,
            "available": False,
            "browser_enabled": cls.BROWSER_ENABLED,
            "sandbox_enabled": cls.SANDBOX_ENABLED,
            "container_status": "not_running"
        }

        if not cls.ENABLED:
            result["container_status"] = "disabled"
            return result

        try:
            # 检查容器状态
            check = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Status}}", cls.CONTAINER_NAME],
                capture_output=True,
                text=True,
                timeout=5
            )

            if check.returncode == 0:
                status = check.stdout.strip()
                result["container_status"] = status
                result["available"] = (status == "running")

        except Exception as e:
            logger.warning(f"容器检查失败: {e}")
            result["container_status"] = "error"

        return result

    @classmethod
    def kill_switch(cls, scope: str = "all"):
        """
        紧急禁用

        Args:
            scope: 禁用范围 (all/browser/sandbox)
        """
        if scope == "all":
            cls.ENABLED = False
            logger.warning("OpenClaw 已全局禁用")
        elif scope == "browser":
            cls.BROWSER_ENABLED = False
            logger.warning("OpenClaw Browser 已禁用")
        elif scope == "sandbox":
            cls.SANDBOX_ENABLED = False
            logger.warning("OpenClaw Sandbox 已禁用")


# 便捷函数
def openclaw_browser(action: str, url: str = "", **kwargs) -> Tuple[bool, str]:
    """便捷函数：执行浏览器任务"""
    return OpenClawAdapter.run_browser(action, url, **kwargs)


def openclaw_sandbox(language: str, code: str, timeout: int = 30) -> Tuple[bool, str]:
    """便捷函数：执行沙箱代码"""
    return OpenClawAdapter.run_sandbox(language, code, timeout)