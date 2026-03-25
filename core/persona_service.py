"""
个人画像服务 (Persona Service)

负责从 Obsidian Vault 中读取用户的个人风格、技术偏好和研究技能 (Skills.md)。
"""

import logging
from typing import Optional, Dict, Any
from .obsidian_client import obsidian_client

logger = logging.getLogger(__name__)


class PersonaService:
    """个人画像服务"""

    DEFAULT_PATH = "70-79_个人笔记_Personal/Skills.md"

    def __init__(self, obsidian_client=obsidian_client):
        self.client = obsidian_client
        self._persona_cache: Optional[Dict[str, Any]] = None

    def get_persona(self, refresh: bool = False) -> str:
        """
        获取格式化的用户画像字符串，用于 LLM 系统提示词补充。
        
        Args:
            refresh: 是否强制刷新缓存
            
        Returns:
            用户画像文本
        """
        if not refresh and self._persona_cache:
            return self._persona_cache.get("content", "")

        logger.info(f"正在从 Obsidian 加载用户画像: {self.DEFAULT_PATH}")
        
        # 1. 尝试直接按路径读取
        result = self.client.read_note(self.DEFAULT_PATH)
        
        # 2. 如果失败，尝试在大分类目录下搜索
        if not result.get("success"):
            logger.warning(f"直接读取失败，尝试搜索 'Skills'...")
            search_result = self.client.search_vault("Skills", folder="70-79_个人笔记_Personal", limit=1)
            if search_result.get("total", 0) > 0:
                best_match = search_result["results"][0]["path"]
                logger.info(f"找到备选路径: {best_match}")
                result = self.client.read_note(best_match)

        if result.get("success"):
            content = result["content"]
            metadata = result["metadata"]
            
            # 格式化
            persona_text = f"### 用户画像与研究偏好\n\n{content}\n"
            if metadata:
                meta_str = "\n".join([f"- {k}: {v}" for k, v in metadata.items()])
                persona_text = f"### 核心技能标签\n{meta_str}\n\n" + persona_text
            
            self._persona_cache = {"content": persona_text, "metadata": metadata}
            return persona_text
        
        logger.warning("未找到有效且非空的用户画像 (Skills.md)")
        return ""

    def get_system_prompt_addon(self) -> str:
        """获取用于注入系统提示词的片段"""
        persona = self.get_persona()
        if not persona:
            return ""
            
        return f"\n【个人研究偏好注入】\n请务必参考以下用户的研究风格和关注重点进行输出：\n\n{persona}\n"


# 全局实例
persona_service = PersonaService()
