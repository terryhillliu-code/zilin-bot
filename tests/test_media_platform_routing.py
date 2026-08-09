#!/usr/bin/env python3
"""extract_video_url / extract_article_url 平台路由测试 — 双向追问改造 Phase 4

覆盖（对应计划验收项）：
1. 9 平台 x 正常/变体链接命中视频路由
2. 非视频 URL / 文章链接不被误判为视频（负例必测）
3. 视频域名不被文章路由捕获
4. unix.com 类域名不被 x.com 误伤（hostname 后缀匹配验证）
5. ⭐ 2026-08-09 行为变更：小红书移交图文管线——xhs/xhslink 链接由 webnote_distiller
   前置分支接管（commands/media_commands.py 0.5 分支，先于视频判断），且
   extract_article_url 不再排除小红书域名（process_article 内可按图片数升级图文管线）；
   extract_video_url 保留 xhs 正则仅作适配器内部回退视频管线用，不再决定入口路由

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

    # ---- ⭐ 2026-08-09 行为变更：小红书移交图文管线（webnote_distiller 专管） ----
    # 入口路由由 commands/media_commands.py 的 0.5 前置分支决定（先于视频分支），
    # 不再由 extract_video_url/is_video_url 吞掉；视频类笔记由适配器内部回退视频管线。
    def test_xiaohongshu_routes_to_webnote(self):
        from webnote_distiller import extract_web_note_url
        for text in ["https://www.xiaohongshu.com/explore/6590abcdef?xsec_token=abc",
                     "推荐 https://xiaohongshu.com/discovery/item/6590abcdef 给你"]:
            url, source = extract_web_note_url(text)
            self.assertIsNotNone(url, f"webnote 分支应捕获: {text}")
            self.assertEqual(source, "xiaohongshu")
        url, source = extract_web_note_url("http://xhslink.com/a/AbCdEf")
        self.assertIsNotNone(url, "xhslink 短链应被 webnote 分支捕获")
        self.assertEqual(source, "xiaohongshu")
        # 文章路由不再排除小红书域名（process_article 内图片≥3 时可升级图文管线）
        self.assertEqual(extract_article_url("https://www.xiaohongshu.com/explore/6590abc"),
                         "https://www.xiaohongshu.com/explore/6590abc")

    def test_zhihu_routes_to_webnote(self):
        from webnote_distiller import extract_web_note_url
        url, source = extract_web_note_url("https://www.zhihu.com/question/12345")
        self.assertIsNotNone(url)
        self.assertEqual(source, "zhihu")
        self.assertNotVideo("https://www.zhihu.com/question/12345")

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
        """视频域名链接不被文章路由捕获（⭐ 2026-08-09 起小红书已移出该断言，
        移交图文管线，见 test_xiaohongshu_routes_to_webnote）"""
        for text in ["https://v.kuaishou.com/AbCd",
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
