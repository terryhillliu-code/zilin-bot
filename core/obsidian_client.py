"""zhiwei-obsidian 服务客户端。

调用 zhiwei-obsidian 服务进行 Markdown 生成、分类和导出。
"""

import httpx
from typing import Any, Dict, List, Optional
from pathlib import Path


class ObsidianClient:
    """Obsidian 导出服务客户端。"""

    def __init__(self, base_url: str = "http://127.0.0.1:8766"):
        """初始化客户端。

        Args:
            base_url: zhiwei-obsidian 服务地址
        """
        self.base_url = base_url
        self.timeout = 30.0

    def is_available(self) -> bool:
        """检查服务是否可用。"""
        try:
            response = httpx.get(f"{self.base_url}/health", timeout=5.0)
            return response.status_code == 200
        except Exception:
            return False

    def classify(self, title: str, content: str = "") -> Dict[str, Any]:
        """对文档进行 JD 分类。

        Args:
            title: 文档标题
            content: 文档内容（可选）

        Returns:
            分类结果：{jd_code, jd_dir, category, is_chinese_report, doc_type}
        """
        try:
            response = httpx.post(
                f"{self.base_url}/classify",
                json={"title": title, "content": content},
                timeout=10.0
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            # 返回默认分类
            return {
                "jd_code": "10-19",
                "jd_dir": str(Path.home() / "Documents" / "ZhiweiVault" / "10-19_AI-Systems"),
                "category": "AI 系统",
                "is_chinese_report": False,
                "doc_type": "电子书",
                "error": str(e)
            }

    def sanitize_filename(self, title: str) -> Dict[str, str]:
        """清理文件名。

        Args:
            title: 原始标题

        Returns:
            {safe_name, suggested_filename}
        """
        try:
            response = httpx.get(
                f"{self.base_url}/naming/sanitize",
                params={"title": title},
                timeout=10.0
            )
            response.raise_for_status()
            return response.json()
        except Exception:
            # 简单的本地回退
            import re
            safe = re.sub(r'[<>:"/\\|?*]', '', title)
            safe = re.sub(r'\s+', '_', safe)[:100]
            return {"safe_name": safe, "suggested_filename": f"{safe}.md"}

    def generate_markdown(
        self,
        title: str,
        content: str,
        metadata: Dict[str, Any],
        template: str = "note"
    ) -> Dict[str, str]:
        """生成 Markdown 内容。

        Args:
            title: 标题
            content: 内容
            metadata: 元数据
            template: 模板类型 (paper/report/note)

        Returns:
            {markdown, yaml}
        """
        try:
            response = httpx.post(
                f"{self.base_url}/markdown/generate",
                json={
                    "title": title,
                    "content": content,
                    "metadata": metadata,
                    "template": template
                },
                timeout=self.timeout
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            return {"error": str(e)}

    def export_report(
        self,
        title: str,
        summary: str,
        source: str = "",
        pages: int = 0,
        doc_type: str = "行业研报",
        attachment_path: Optional[str] = None,
    ) -> Dict[str, str]:
        """导出研报到 Obsidian。

        Args:
            title: 报告标题
            summary: 摘要内容
            source: 来源
            pages: 页数
            doc_type: 文档类型
            attachment_path: 附件路径

        Returns:
            {md_path, jd_dir, success, error}
        """
        payload = {
            "type": "report",
            "metadata": {
                "title": title,
                "source": source,
                "pages": pages,
                "doc_type": doc_type,
            },
            "content": {"summary": summary},
        }

        if attachment_path:
            payload["attachment_path"] = attachment_path

        try:
            response = httpx.post(
                f"{self.base_url}/export/obsidian",
                json=payload,
                timeout=self.timeout
            )
            response.raise_for_status()
            result = response.json()

            if result.get("success"):
                return {
                    "md_path": result.get("md_path", ""),
                    "jd_dir": result.get("jd_dir", ""),
                    "success": True,
                }
            else:
                return {"success": False, "error": result.get("error", "导出失败")}

        except Exception as e:
            return {"success": False, "error": str(e)}

    def export_paper(
        self,
        title: str,
        source_url: str,
        date: str,
        summary: str,
        tags: List[str] = None,
        tier: str = "B",
        overall_rating: str = "B",
        authors: List[str] = None,
        institutions: List[str] = None,
        one_line_summary: str = "",
        knowledge_links: List[str] = None,
        action_items: List[str] = None,
        attachment_path: Optional[str] = None,
    ) -> Dict[str, str]:
        """导出论文到 Obsidian。

        Args:
            title: 论文标题
            source_url: 来源 URL
            date: 发布日期
            summary: 分析报告
            tags: 标签列表
            tier: 内容等级
            overall_rating: 综合评级
            authors: 作者列表
            institutions: 机构列表
            one_line_summary: 一句话总结
            knowledge_links: 知识关联
            action_items: 行动建议
            attachment_path: PDF 附件路径

        Returns:
            {md_path, jd_dir, success, error}
        """
        payload = {
            "type": "paper",
            "metadata": {
                "title": title,
                "source_url": source_url,
                "date": date,
                "tags": tags or [],
                "tier": tier,
                "overall_rating": overall_rating,
                "authors": authors or [],
                "institutions": institutions or [],
                "one_line_summary": one_line_summary,
                "knowledge_links": knowledge_links or [],
                "action_items": action_items or [],
            },
            "content": {"report": summary},
        }

        if attachment_path:
            payload["attachment_path"] = attachment_path

        try:
            response = httpx.post(
                f"{self.base_url}/export/obsidian",
                json=payload,
                timeout=self.timeout
            )
            response.raise_for_status()
            result = response.json()

            if result.get("success"):
                return {
                    "md_path": result.get("md_path", ""),
                    "jd_dir": result.get("jd_dir", ""),
                    "success": True,
                }
            else:
                return {"success": False, "error": result.get("error", "导出失败")}

        except Exception as e:
            return {"success": False, "error": str(e)}

    def export_note(
        self,
        title: str,
        content: str,
        source: str = "",
        tags: List[str] = None,
    ) -> Dict[str, str]:
        """导出通用笔记到 Obsidian。

        Args:
            title: 标题
            content: 内容
            source: 来源
            tags: 标签列表

        Returns:
            {md_path, jd_dir, success, error}
        """
        payload = {
            "type": "note",
            "metadata": {
                "title": title,
                "source": source,
                "tags": tags or [],
            },
            "content": {"text": content},
        }

        try:
            response = httpx.post(
                f"{self.base_url}/export/obsidian",
                json=payload,
                timeout=self.timeout
            )
            response.raise_for_status()
            result = response.json()

            if result.get("success"):
                return {
                    "md_path": result.get("md_path", ""),
                    "jd_dir": result.get("jd_dir", ""),
                    "success": True,
                }
            else:
                return {"success": False, "error": result.get("error", "导出失败")}

        except Exception as e:
            return {"success": False, "error": str(e)}

    def copy_attachment(self, source_path: str, dest_name: str = None) -> Dict[str, str]:
        """复制附件到 Obsidian 附件目录。"""
        try:
            response = httpx.post(
                f"{self.base_url}/attachments/copy",
                json={"source_path": source_path, "dest_name": dest_name},
                timeout=self.timeout
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            return {"success": False, "error": str(e)}

    def search_vault(self, query: str, folder: str = None, limit: int = 10) -> Dict[str, Any]:
        """全文检索 Vault。"""
        try:
            response = httpx.post(
                f"{self.base_url}/vault/search",
                json={"query": query, "folder": folder, "limit": limit},
                timeout=self.timeout
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            return {"results": [], "total": 0, "error": str(e)}

    def read_note(self, path: str) -> Dict[str, Any]:
        """读取笔记内容。"""
        try:
            response = httpx.post(
                f"{self.base_url}/vault/read",
                json={"path": path},
                timeout=self.timeout
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            return {"success": False, "content": "", "metadata": {}, "error": str(e)}


# 全局客户端实例
obsidian_client = ObsidianClient()