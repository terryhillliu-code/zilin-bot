"""
统一 LLM 客户端
替代 OpenClaw Agent 调用，直连百炼 API

功能：
- 多模型切换（知微/探微/通微角色映射）
- 自动降级（qwen3.5-plus → glm-5）
- 复用 8045 本地代理或直连百炼 API
- 兼容原 call_openclaw_agent 签名

使用：
    from llm_client import llm_client

    # 简单调用
    result = llm_client.call("chat", "你好")

    # 带系统提示词
    result = llm_client.call("research", "分析这段文本", system_prompt="你是分析专家")

    # 兼容原 OpenClaw 签名
    result = llm_client.call_with_session("chat", "你好", "session-123")
"""
import os
import json
import http.client
import logging
from typing import Optional, Tuple
from dataclasses import dataclass
from pathlib import Path

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class LLMConfig:
    """LLM 配置"""
    # 本地代理配置（LLM 代理服务，需单独启动）
    proxy_host: str = "127.0.0.1"
    proxy_port: int = 8045  # LLM 代理端口（非 RAG 服务的 8765）

    # 百炼 API 直连配置（代理不可用时降级使用）
    bailian_api_key: Optional[str] = None
    bailian_base_url: str = "coding.dashscope.aliyuncs.com"

    # 超时设置
    default_timeout: int = 120
    max_timeout: int = 600

    # 是否优先使用直连 API（跳过代理）
    prefer_direct: bool = True  # 默认直连百炼，避免代理依赖


class LLMClient:
    """
    统一 LLM 客户端

    支持多模型切换和自动降级
    """

    # 角色到模型的映射
    ROLE_MODELS = {
        "chat": "qwen3.5-plus",      # 知微 - 对话
        "research": "kimi-k2.5",     # 探微 - 研究
        "format": "qwen3.5-plus",    # 通微 - 格式化
        "main": "qwen3.5-plus",      # 兼容 OpenClaw main agent
        "researcher": "kimi-k2.5",   # 兼容 OpenClaw researcher agent
        "operator": "qwen3.5-plus",  # 兼容 OpenClaw operator agent
    }

    # 模型降级链
    FALLBACK_CHAIN = {
        "qwen3.5-plus": ["glm-5", "MiniMax-M2.5"],
        "kimi-k2.5": ["qwen3.5-plus", "glm-5"],
        "glm-5": ["MiniMax-M2.5"],
    }

    # 角色系统提示词
    ROLE_PROMPTS = {
        "chat": "你是知微，一个友好、专业的AI助手。",
        "research": "你是探微，一个擅长深度分析和信息收集的AI助手。",
        "format": "你是通微，一个擅长内容整理和格式化的AI助手。",
        "main": "你是知微，一个友好、专业的AI助手。",
        "researcher": "你是探微，一个擅长深度分析和信息收集的AI助手。",
        "operator": "你是通微，一个擅长内容整理和格式化的AI助手。",
    }

    def __init__(self, config: Optional[LLMConfig] = None):
        self.config = config or LLMConfig()
        # 从多个来源加载 API Key
        self.config.bailian_api_key = (
            self.config.bailian_api_key or
            os.getenv("BAILIAN_API_KEY") or
            self._load_api_key_from_env()
        )

    def _load_api_key_from_env(self) -> Optional[str]:
        """从 zhiwei-bot/.env 文件加载 API Key"""
        env_paths = [
            Path(__file__).parent / ".env",  # zhiwei-bot/.env
            Path.home() / "zhiwei-bot" / ".env",
        ]
        for env_path in env_paths:
            if env_path.exists():
                try:
                    with open(env_path) as f:
                        for line in f:
                            line = line.strip()
                            if line.startswith("BAILIAN_API_KEY="):
                                return line.split("=", 1)[1]
                except Exception as e:
                    logger.warning(f"读取 .env 失败: {e}")
        return None

    def call(
        self,
        role: str,
        message: str,
        system_prompt: Optional[str] = None,
        timeout: Optional[int] = None
    ) -> Tuple[bool, str]:
        """
        统一调用接口

        Args:
            role: 角色 (chat/research/format/main/researcher/operator)
            message: 用户消息
            system_prompt: 可选系统提示词
            timeout: 超时时间（秒）

        Returns:
            (success, content) 元组
        """
        model = self.ROLE_MODELS.get(role, "qwen3.5-plus")
        system = system_prompt or self.ROLE_PROMPTS.get(role, "")
        timeout = timeout or self.config.default_timeout

        # 尝试调用，失败时降级
        fallback_models = [model] + self.FALLBACK_CHAIN.get(model, [])

        for current_model in fallback_models:
            # 优先直连百炼 API（避免代理依赖）
            if self.config.prefer_direct and self.config.bailian_api_key:
                try:
                    success, content = self._call_via_bailian(
                        model=current_model,
                        system_prompt=system,
                        message=message,
                        timeout=timeout
                    )
                    if success:
                        return True, content
                    logger.warning(f"百炼直连 {current_model} 失败，尝试降级...")
                except Exception as e:
                    logger.warning(f"百炼直连 {current_model} 异常: {e}")

            # 降级：通过代理调用
            try:
                success, content = self._call_via_proxy(
                    model=current_model,
                    system_prompt=system,
                    message=message,
                    timeout=timeout
                )
                if success:
                    return True, content
                logger.warning(f"代理 {current_model} 失败，尝试降级...")
            except Exception as e:
                logger.warning(f"代理 {current_model} 异常: {e}")

        # 所有模型都失败
        return False, "所有模型调用失败，请稍后重试"

    def call_with_session(
        self,
        role: str,
        message: str,
        session_id: str,
        system_prompt: Optional[str] = None,
        timeout: Optional[int] = None
    ) -> str:
        """
        带会话 ID 调用，兼容原 call_openclaw_agent 签名

        Args:
            role: 角色
            message: 用户消息
            session_id: 会话 ID（当前实现中未使用，预留）
            system_prompt: 可选系统提示词
            timeout: 超时时间

        Returns:
            响应字符串（失败时返回友好提示）
        """
        # TODO: 实现 session 记忆管理
        # 当前 session_id 仅用于日志追踪
        logger.info(f"会话 {session_id} - 角色 {role}")

        timeout = timeout or 300  # 兼容原 OpenClaw 默认超时
        success, content = self.call(role, message, system_prompt, timeout)

        if success:
            return content
        else:
            return "❌ AI 暂时无法响应，请稍后重试"

    def _call_via_proxy(
        self,
        model: str,
        system_prompt: str,
        message: str,
        timeout: int
    ) -> Tuple[bool, str]:
        """
        通过本地代理调用 LLM

        Args:
            model: 模型名称
            system_prompt: 系统提示词
            message: 用户消息
            timeout: 超时时间

        Returns:
            (success, content) 元组
        """
        messages = []

        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        messages.append({"role": "user", "content": message})

        payload = json.dumps({
            "model": model,
            "messages": messages,
            "temperature": 0.7
        })

        try:
            conn = http.client.HTTPConnection(
                self.config.proxy_host,
                self.config.proxy_port,
                timeout=timeout
            )
            conn.request(
                "POST",
                "/v1/chat/completions",
                body=payload,
                headers={"Content-Type": "application/json"}
            )
            resp = conn.getresponse()
            data = json.loads(resp.read().decode())
            conn.close()

            if resp.status == 200:
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                logger.info(f"模型 {model} 响应成功，{len(content)} 字符")
                return True, content
            else:
                err = data.get("error", {}).get("message", "Unknown error")
                logger.error(f"模型 {model} 返回错误 {resp.status}: {err}")
                return False, f"LLM Error {resp.status}: {err}"

        except http.client.HTTPException as e:
            logger.error(f"HTTP 异常: {e}")
            return False, f"HTTP Exception: {e}"
        except Exception as e:
            logger.error(f"调用异常: {e}")
            return False, f"Exception: {e}"

    def _call_via_bailian(
        self,
        model: str,
        system_prompt: str,
        message: str,
        timeout: int
    ) -> Tuple[bool, str]:
        """
        直连百炼 API（代理不可用时降级使用）

        Args:
            model: 模型名称
            system_prompt: 系统提示词
            message: 用户消息
            timeout: 超时时间

        Returns:
            (success, content) 元组
        """
        if not self.config.bailian_api_key:
            return False, "未配置百炼 API Key"

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": message})

        payload = json.dumps({
            "model": model,
            "messages": messages,
            "temperature": 0.7
        })

        try:
            import ssl
            import urllib.request

            context = ssl.create_default_context()
            url = f"https://{self.config.bailian_base_url}/v1/chat/completions"

            req = urllib.request.Request(
                url,
                data=payload.encode('utf-8'),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.config.bailian_api_key}"
                },
                method='POST'
            )

            with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
                data = json.loads(resp.read().decode())

            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            logger.info(f"百炼直连 {model} 成功，{len(content)} 字符")
            return True, content

        except Exception as e:
            logger.error(f"百炼直连异常: {e}")
            return False, f"Bailian Exception: {e}"


# 全局单例
llm_client = LLMClient()


# 兼容函数（方便直接导入使用）
def call_llm(role: str, message: str, **kwargs) -> Tuple[bool, str]:
    """快捷调用函数"""
    return llm_client.call(role, message, **kwargs)


def call_llm_with_session(role: str, message: str, session_id: str, **kwargs) -> str:
    """带会话的快捷调用函数"""
    return llm_client.call_with_session(role, message, session_id, **kwargs)


# 测试
if __name__ == "__main__":
    print("=== 测试 LLM 客户端 ===")

    # 测试简单调用
    success, content = llm_client.call("chat", "你好，请用一句话介绍自己")
    print(f"成功: {success}")
    print(f"响应: {content[:100]}..." if len(content) > 100 else f"响应: {content}")

    # 测试带 session 调用
    result = llm_client.call_with_session("chat", "1+1等于几？", "test-session-001")
    print(f"带 session 响应: {result[:100]}..." if len(result) > 100 else f"带 session 响应: {result}")