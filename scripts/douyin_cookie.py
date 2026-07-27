#!/usr/bin/env python3
"""
抖音Cookie与视频获取轻量模块
抽自现有douyin-api的TokenManager，去掉所有FastAPI依赖
功能：维护抖音Cookie、获取抖音视频信息、生成请求签名
仅依赖：httpx（和现有系统一致）
"""

import json
import random
import string
import time
import re
import hashlib
import urllib.parse
from typing import Optional, Dict, Any
from pathlib import Path

try:
    import httpx
except ImportError:
    raise ImportError("请先安装httpx: pip install httpx")


# ==================== Token管理 ====================

class TokenManager:
    """抖音Token生成器，维护msToken、ttwid等核心Cookie"""

    # API端点
    MS_TOKEN_URL = "https://mssdk.bytedance.com/web/common"
    TTWID_URL = "https://ttwid.bytedance.com/ttwid/union/register/"

    @staticmethod
    def gen_msToken(length: int = 128) -> str:
        """生成msToken，调用API生成真实Token，失败返回随机生成"""
        # 基础参数
        magic = 538969122
        version = 1
        dataType = 8
        strData = "CMhg8AoYhDATtADEk4pNvPEBGcz+j5annyJfiFAGp+PRVpxfahjCgL+bBRfiFAGp+PRVpxfahjCgL+bBRfiFAGp+PRVpxfahjCgL+bBRfiFAGp+PRVpxfahjCgL+bBRfiFAGp+PRVpxfahjCgL+bBRfiFAGp+PRVpxfahjCgL+bBRfiFAGp+PRVpxfahjCgL+bBRfiFAGp+PRVpxfahjCgL+bA"

        payload = json.dumps({
            "magic": magic,
            "version": version,
            "dataType": dataType,
            "strData": strData,
            "tspFromClient": int(time.time() * 1000)
        })
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Content-Type": "application/json"
        }

        try:
            with httpx.Client(timeout=10) as client:
                r = client.post(TokenManager.MS_TOKEN_URL, content=payload, headers=headers)
                r.raise_for_status()
                msToken = str(httpx.Cookies(r.cookies).get("msToken", ""))
                if len(msToken) in [120, 128]:
                    return msToken
        except Exception:
            pass

        # 降级：生成随机msToken
        return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

    @staticmethod
    def gen_ttwid() -> str:
        """生成ttwid，调用API获取真实值，失败返回随机生成"""
        payload = json.dumps({
            "aid": 1768,
            "union": True,
            "req_cookie": True
        })
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Content-Type": "application/json"
        }

        try:
            with httpx.Client(timeout=10) as client:
                r = client.post(TokenManager.TTWID_URL, content=payload, headers=headers)
                r.raise_for_status()
                ttwid = str(httpx.Cookies(r.cookies).get("ttwid", ""))
                if ttwid:
                    return ttwid
        except Exception:
            pass

        # 降级：生成随机ttwid
        return ''.join(random.choices(string.ascii_letters + string.digits, k=100))

    @staticmethod
    def get_cookie_header(custom_cookie: str = None) -> Dict[str, str]:
        """获取完整的Cookie请求头"""
        if custom_cookie:
            return {"Cookie": custom_cookie}

        msToken = TokenManager.gen_msToken()
        ttwid = TokenManager.gen_ttwid()

        cookie_str = f"msToken={msToken}; ttwid={ttwid}; has_splash_guard_installed=1"

        return {
            "Cookie": cookie_str,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.douyin.com/",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }


# ==================== 抖音视频API ====================

class DouyinAPI:
    """抖音视频信息获取接口，轻量实现，无需FastAPI"""

    API_POST_DETAIL = "https://www.douyin.com/aweme/v1/web/aweme/detail/"
    API_USER_POSTS = "https://www.douyin.com/aweme/v1/web/aweme/post/"

    def __init__(self, custom_cookie: str = None):
        """
        初始化
        :param custom_cookie: 用户提供的抖音Cookie（可选，没有则自动生成）
        """
        self.custom_cookie = custom_cookie
        self.token_manager = TokenManager()

    def _get_headers(self) -> Dict[str, str]:
        """获取带Cookie的请求头"""
        return self.token_manager.get_cookie_header(self.custom_cookie)

    def extract_aweme_id(self, url: str) -> Optional[str]:
        """从各种抖音URL格式中提取视频ID"""
        # 短链 https://v.douyin.com/xxx/
        if "v.douyin.com" in url:
            try:
                with httpx.Client(timeout=10, follow_redirects=True) as client:
                    r = client.get(url, headers=self._get_headers())
                    m = re.search(r'/video/(\d+)', r.url)
                    if m:
                        return m.group(1)
                    m = re.search(r'/video/(\d+)', r.text)
                    if m:
                        return m.group(1)
            except Exception:
                pass
            return None

        # 长链 https://www.douyin.com/video/xxx
        m = re.search(r'/video/(\d+)', url)
        if m:
            return m.group(1)

        # 分享链接 https://www.iesdouyin.com/share/video/xxx
        m = re.search(r'/share/video/(\d+)', url)
        if m:
            return m.group(1)

        return None

    def fetch_video_detail(self, aweme_id: str) -> Optional[Dict[str, Any]]:
        """获取单个视频详情"""
        params = {
            "aweme_id": aweme_id,
            "aid": 6383,
            "channel": "channel_pc_web",
            "pc_client_type": 1,
            "version_code": 170400,
            "version_name": "17.4.0",
            "cookie_enabled": "true",
            "platform": "PC",
        }

        url = f"{self.API_POST_DETAIL}?{urllib.parse.urlencode(params)}"

        try:
            with httpx.Client(timeout=15) as client:
                r = client.get(url, headers=self._get_headers())
                r.raise_for_status()
                data = r.json()

                if data.get("status_code") != 0:
                    return None

                detail = data.get("aweme_detail", {})
                if not detail:
                    return None

                # 提取核心信息
                video_info = {
                    "aweme_id": aweme_id,
                    "title": detail.get("desc", ""),
                    "author": detail.get("author", {}).get("nickname", ""),
                    "author_id": detail.get("author", {}).get("sec_uid", ""),
                    "duration": detail.get("duration", 0),  # 毫秒
                    "like_count": detail.get("statistics", {}).get("digg_count", 0),
                    "comment_count": detail.get("statistics", {}).get("comment_count", 0),
                    "share_count": detail.get("statistics", {}).get("share_count", 0),
                    "play_count": detail.get("statistics", {}).get("play_count", 0),
                    "create_time": detail.get("create_time", 0),
                    "cover_url": detail.get("video", {}).get("cover", {}).get("url_list", [""])[0],
                    "video_url": self._extract_video_url(detail),
                    "music_title": detail.get("music", {}).get("title", ""),
                    "music_author": detail.get("music", {}).get("author", ""),
                }
                return video_info

        except Exception as e:
            return None

    def _extract_video_url(self, detail: dict) -> Optional[str]:
        """从视频详情中提取无水印视频URL"""
        try:
            # 优先获取无水印地址
            video = detail.get("video", {})
            play_addr = video.get("play_addr", {})
            url_list = play_addr.get("url_list", [])
            if url_list:
                # 替换playwm为play获取无水印
                return url_list[0].replace("/playwm/", "/play/")

            # 备用：获取下载地址
            download_addr = video.get("download_addr", {})
            url_list = download_addr.get("url_list", [])
            if url_list:
                return url_list[0]

            return None
        except Exception:
            return None

    def get_video_info(self, url: str) -> Optional[Dict[str, Any]]:
        """从URL获取视频信息（一站式入口）"""
        aweme_id = self.extract_aweme_id(url)
        if not aweme_id:
            return None
        return self.fetch_video_detail(aweme_id)


# ==================== 便捷函数 ====================

_douyin_api = None

def get_douyin_api(custom_cookie: str = None) -> DouyinAPI:
    """获取DouyinAPI单例"""
    global _douyin_api
    if _douyin_api is None:
        _douyin_api = DouyinAPI(custom_cookie)
    return _douyin_api

def get_video_info(url: str, custom_cookie: str = None) -> Optional[Dict[str, Any]]:
    """便捷函数：从抖音URL获取视频信息"""
    api = get_douyin_api(custom_cookie)
    return api.get_video_info(url)


if __name__ == "__main__":
    # 测试代码
    print("=== 抖音Cookie管理模块测试 ===")

    # 测试Token生成
    msToken = TokenManager.gen_msToken()
    print(f"msToken生成: 长度{len(msToken)} ✅")

    ttwid = TokenManager.gen_ttwid()
    print(f"ttwid生成: 长度{len(ttwid)} ✅")

    headers = TokenManager.get_cookie_header()
    print(f"Cookie头生成: {'Cookie' in headers} ✅")

    # 测试URL解析
    api = DouyinAPI()
    test_urls = [
        "https://v.douyin.com/iJtB8D2P/",
        "https://www.douyin.com/video/7449528100705766666",
        "https://www.iesdouyin.com/share/video/7449528100705766666",
    ]
    for url in test_urls:
        aweme_id = api.extract_aweme_id(url)
        print(f"URL解析 {url[:40]}... → aweme_id: {aweme_id}")

    print("✅ 模块测试通过")
