#!/usr/bin/env python3
"""extract_video_url / extract_article_url 平台路由测试 — 双向追问改造 Phase 4

覆盖（对应计划验收项）：
1. 9 平台 x 正常/变体链接命中视频路由
2. 非视频 URL / 文章链接不被误判为视频（负例必测）
3. 新平台视频域名不再被文章路由捕获
4. unix.com 类域名不被 x.com 误伤（hostname 后缀匹配验证）

运行：~/zhiwei-shared-venv/bin/python3 -m unittest tests.test_media_platform_routing -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from media_handler import extract_video_url, extract_article_url, is_video_url  # noqa: E402


class TestVideoUrlExtraction(unittest.TestCase):

    def assertVideo(self, text, expected_sub=None):
        url = extract_video_url(text)
        self.assertIsNotNone(url, f"应识别为视频: {text}")
        if expected_sub:
            self.assertIn(expected_sub, url)

    def assertNotVideo(self, text):
        self.assertIsNone(extract_video_url(text), f"不应识别为视频: {text}")

    # ---- 既有 4 平台回归 ----
    def test_legacy_platforms(self):
        self.assertVideo("https://v.douyin.com/iRNBho6u/", "v.douyin.com")
        self.assertVideo("https://www.douyin.com/video/7301234567890", "douyin.com/video")
        self.assertVideo("https://www.iesdouyin.com/share/video/7301234567890", "douyin.com/video/7301234567890")
        self.assertVideo("看看这个 https://www.youtube.com/watch?v=abc123XYZ_- 不错", "watch?v=abc123XYZ_-")
        self.assertVideo("https://youtu.be/abc123", "youtu.be")
        self.assertVideo("https://www.youtube.com/shorts/abc123", "shorts")
        self.assertVideo("https://www.bilibili.com/video/BV1xx411c7mD", "BV1xx411c7mD")
        self.assertVideo("https://b23.tv/abc123", "b23.tv")

    # ---- P4 新增 5 平台 ----
    def test_xiaohongshu(self):
        self.assertVideo("https://www.xiaohongshu.com/explore/6590abcdef?xsec_token=abc", "xiaohongshu.com")
        self.assertVideo("http://xhslink.com/a/AbCdEf", "xhslink.com")
        self.assertVideo("推荐 https://xiaohongshu.com/discovery/item/6590abcdef 给你", "xiaohongshu.com")

    def test_kuaishou(self):
        self.assertVideo("https://www.kuaishou.com/short-video/3xf8abc", "kuaishou.com")
        self.assertVideo("https://v.kuaishou.com/AbCdEf", "v.kuaishou.com")

    def test_weibo(self):
        self.assertVideo("https://weibo.com/tv/show/1034:5012345", "weibo.com/tv/show")
        self.assertVideo("https://www.weibo.com/1234567890/OxYz123ab", "weibo.com")
        self.assertVideo("https://m.weibo.cn/detail/5012345678", "weibo.cn")

    def test_tiktok(self):
        self.assertVideo("https://www.tiktok.com/@user123/video/7301234567890123456", "tiktok.com")
        self.assertVideo("https://vm.tiktok.com/ZM8abc/", "vm.tiktok.com")

    def test_x_twitter(self):
        self.assertVideo("https://x.com/elonmusk/status/1801234567890123456", "x.com")
        self.assertVideo("https://twitter.com/user/status/1801234567890?s=20", "twitter.com")

    # ---- 负例：非视频不得误判 ----
    def test_negative_cases(self):
        self.assertNotVideo("今天天气不错")
        self.assertNotVideo("https://mp.weixin.qq.com/s/AbCdEfG")
        self.assertNotVideo("https://unix.com/articles/some-post")  # x.com 子串陷阱
        self.assertNotVideo("https://www.zhihu.com/question/12345")
        self.assertNotVideo("https://example.com/page")
        # 小红书用户主页不是视频页（yt-dlp 探测失败时由 distiller 报错兜底，这里仅验证不误入视频正则）
        self.assertNotVideo("https://weibo.com/u/1234567890")

    def test_article_route_not_steal_video_domains(self):
        """新平台视频链接不再被文章路由捕获"""
        for text in ["https://www.xiaohongshu.com/explore/6590abc",
                     "https://v.kuaishou.com/AbCd",
                     "https://x.com/user/status/180123"]:
            self.assertIsNone(extract_article_url(text), f"文章路由不应捕获: {text}")
            self.assertTrue(is_video_url(text))

    def test_article_route_still_works(self):
        self.assertEqual(extract_article_url("https://mp.weixin.qq.com/s/AbCd"),
                         "https://mp.weixin.qq.com/s/AbCd")
        self.assertEqual(extract_article_url("读读 https://unix.com/articles/x 这篇"),
                         "https://unix.com/articles/x")
        self.assertIsNone(extract_article_url("https://www.bilibili.com/video/BV1xx"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
