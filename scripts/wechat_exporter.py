"""
微信公众号文章下载器 - 知微系统集成

通过 wechat-article-exporter Docker 服务下载微信公众号文章。
需要先登录并获取 API 密钥。

使用方法:
    from wechat_exporter import WeChatExporter

    exporter = WeChatExporter(auth_key="your-auth-key")

    # 搜索公众号
    accounts = exporter.search_accounts("关键词")

    # 获取文章列表
    articles = exporter.get_articles(fakeid="xxx")

    # 下载文章
    content = exporter.get_article_content(url="xxx")
"""

import os
from dataclasses import dataclass
from typing import List, Optional

import httpx


@dataclass
class WeChatAccount:
    """公众号信息"""
    fakeid: str       # 公众号唯一标识
    nickname: str     # 公众号名称
    alias: str        # 公众号别名
    round_head_img: str  # 头像


@dataclass
class WeChatArticle:
    """文章信息"""
    aid: str          # 文章 ID
    appmsgid: str     # 消息 ID
    title: str        # 标题
    link: str         # 链接
    create_time: int  # 创建时间戳
    author: str       # 作者
    digest: str       # 摘要


class WeChatExporter:
    """微信公众号文章导出器"""

    def __init__(
        self,
        base_url: str = "http://localhost:3001",
        auth_key: Optional[str] = None
    ):
        """
        初始化导出器

        Args:
            base_url: 服务地址，默认本地部署
            auth_key: API 密钥，从 Web 界面获取
        """
        self.base_url = base_url.rstrip("/")
        self.auth_key = auth_key or os.environ.get("WECHAT_AUTH_KEY", "")
        self.client = httpx.Client(timeout=30.0)

    def _headers(self) -> dict:
        """构建请求头"""
        headers = {"Content-Type": "application/json"}
        if self.auth_key:
            headers["X-Auth-Key"] = self.auth_key
        return headers

    def health_check(self) -> bool:
        """检查服务是否可用"""
        try:
            resp = self.client.get(f"{self.base_url}/")
            return resp.status_code == 200
        except Exception:
            return False

    def search_accounts(self, keyword: str) -> List[WeChatAccount]:
        """
        搜索公众号

        Args:
            keyword: 搜索关键词

        Returns:
            公众号列表
        """
        resp = self.client.get(
            f"{self.base_url}/api/accounts/search",
            params={"keyword": keyword},
            headers=self._headers()
        )
        resp.raise_for_status()

        data = resp.json()
        accounts = []

        for item in data.get("list", []):
            accounts.append(WeChatAccount(
                fakeid=item.get("fakeid", ""),
                nickname=item.get("nickname", ""),
                alias=item.get("alias", ""),
                round_head_img=item.get("round_head_img", "")
            ))

        return accounts

    def get_articles(
        self,
        fakeid: str,
        begin: int = 0,
        count: int = 10,
        type: int = 9  # 9=全部, 1=图文
    ) -> List[WeChatArticle]:
        """
        获取公众号文章列表

        Args:
            fakeid: 公众号唯一标识
            begin: 起始位置
            count: 数量（最大 10）
            type: 文章类型

        Returns:
            文章列表
        """
        resp = self.client.get(
            f"{self.base_url}/api/articles",
            params={
                "fakeid": fakeid,
                "begin": begin,
                "count": count,
                "type": type
            },
            headers=self._headers()
        )
        resp.raise_for_status()

        data = resp.json()
        articles = []

        for item in data.get("app_msg_list", []):
            articles.append(WeChatArticle(
                aid=item.get("aid", ""),
                appmsgid=item.get("appmsgid", ""),
                title=item.get("title", ""),
                link=item.get("link", ""),
                create_time=item.get("create_time", 0),
                author=item.get("author", ""),
                digest=item.get("digest", "")
            ))

        return articles

    def get_article_content(self, url: str) -> dict:
        """
        获取文章内容

        Args:
            url: 文章链接

        Returns:
            文章内容字典，包含 title, content, author 等
        """
        resp = self.client.get(
            f"{self.base_url}/api/article/content",
            params={"url": url},
            headers=self._headers()
        )
        resp.raise_for_status()
        return resp.json()

    def download_article(
        self,
        url: str,
        format: str = "markdown"
    ) -> str:
        """
        下载文章为指定格式

        Args:
            url: 文章链接
            format: 输出格式 (html/txt/markdown/json)

        Returns:
            格式化的文章内容
        """
        content = self.get_article_content(url)

        if format == "markdown":
            return self._to_markdown(content)
        elif format == "txt":
            return self._to_text(content)
        else:
            return content.get("content", "")

    def _to_markdown(self, content: dict) -> str:
        """转换为 Markdown 格式"""
        title = content.get("title", "")
        author = content.get("author", "")
        text = content.get("content", "")

        md = f"# {title}\n\n"
        if author:
            md += f"作者: {author}\n\n"
        md += "---\n\n"
        md += text

        return md

    def _to_text(self, content: dict) -> str:
        """转换为纯文本格式"""
        import re
        text = content.get("content", "")
        # 移除 HTML 标签
        text = re.sub(r"<[^>]+>", "", text)
        # 移除多余空白
        text = re.sub(r"\s+", " ", text)
        return text.strip()


# ==================== 测试入口 ====================

def _test():
    """测试连接"""
    exporter = WeChatExporter()

    print("=== 测试服务连接 ===")
    if exporter.health_check():
        print("✅ 服务可用")
    else:
        print("❌ 服务不可用，请检查容器是否运行")
        print("   docker ps | grep wechat")
        return

    if not exporter.auth_key:
        print("\n⚠️ 未配置 API 密钥")
        print("请先访问 http://localhost:3001 登录并获取 API 密钥")
        print("然后设置环境变量: export WECHAT_AUTH_KEY='your-key'")
        return

    print("\n=== 搜索公众号 ===")
    try:
        accounts = exporter.search_accounts("test")
        print(f"找到 {len(accounts)} 个公众号")
        for acc in accounts[:3]:
            print(f"  - {acc.nickname} ({acc.fakeid})")
    except Exception as e:
        print(f"搜索失败: {e}")


if __name__ == "__main__":
    _test()