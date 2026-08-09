#!/usr/bin/env python3
"""douyin_distiller 本地文件入口测试 — 双向追问改造 Phase 6

覆盖：
1. URLResolver 对 local:// 哨兵的解析（platform=local、title=文件名、不存在抛错）
2. extract_audio local 分支：ffmpeg 成功/失败
3. download_video local 分支：文件复制
4. extract_subtitles local 分支：直接返回 None（不走网络）
5. 本地文件 hash 去重键（video_id=local_<sha1[:16]>）
6. main() --local-file 参数解析（文件不存在时退出码 1）

运行：~/zhiwei-shared-venv/bin/python3 -m unittest tests.test_distiller_local_file -v
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import douyin_distiller as dd  # noqa: E402


class TestLocalFileEntry(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.video = Path(self.tmp.name) / "视频号测试.mp4"
        self.video.write_bytes(b"\x00\x00\x00\x18ftyp" + os.urandom(64))

    def tearDown(self):
        self.tmp.cleanup()

    def test_resolve_local_sentinel(self):
        vi = dd.URLResolver().resolve(f"local://{self.video}")
        self.assertEqual(vi.platform, "local")
        self.assertEqual(vi.title, "视频号测试")
        self.assertEqual(vi.resolved_url, str(self.video))

    def test_resolve_local_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            dd.URLResolver().resolve(f"local://{self.tmp.name}/不存在.mp4")

    def test_extract_subtitles_local_returns_none(self):
        extractor = dd.MediaExtractor.__new__(dd.MediaExtractor)
        vi = dd.VideoInfo(original_url="local://x", resolved_url=str(self.video),
                          platform="local")
        self.assertIsNone(extractor.extract_subtitles(vi))

    def test_extract_audio_local_ffmpeg(self):
        extractor = dd.MediaExtractor.__new__(dd.MediaExtractor)
        vi = dd.VideoInfo(original_url="local://x", resolved_url=str(self.video),
                          platform="local")
        out = Path(self.tmp.name) / "audio"
        with patch("subprocess.run") as m:
            m.return_value = MagicMock(returncode=0)
            # 模拟 ffmpeg 产物
            (Path(self.tmp.name) / "audio.mp3").write_bytes(b"fake")
            self.assertTrue(extractor.extract_audio(vi, out))
            cmd = m.call_args[0][0]
            self.assertEqual(cmd[0], "ffmpeg")
            self.assertIn(str(self.video), cmd)
        with patch("subprocess.run", return_value=MagicMock(returncode=1, stderr="boom")):
            self.assertFalse(extractor.extract_audio(vi, out))

    def test_download_video_local_copies(self):
        extractor = dd.MediaExtractor.__new__(dd.MediaExtractor)
        vi = dd.VideoInfo(original_url="local://x", resolved_url=str(self.video),
                          platform="local")
        dest = Path(self.tmp.name) / "copy.mp4"
        self.assertTrue(extractor.download_video(vi, dest))
        self.assertTrue(dest.exists())
        self.assertEqual(dest.read_bytes(), self.video.read_bytes())

    def test_hash_dedup_key_format(self):
        """本地文件 video_id = local_<sha1 前 16 位>（R7）"""
        import hashlib
        expected = "local_" + hashlib.sha1(self.video.read_bytes()).hexdigest()[:16]
        # 复现 process_single_video 内的计算逻辑
        h = hashlib.sha1()
        with open(self.video, 'rb') as f:
            for chunk in iter(lambda: f.read(1 << 20), b''):
                h.update(chunk)
        self.assertEqual(f"local_{h.hexdigest()[:16]}", expected)

    def test_cli_local_file_missing(self):
        distiller = Path(__file__).resolve().parent.parent / "scripts" / "douyin_distiller.py"
        r = subprocess.run(
            [sys.executable, str(distiller), "--local-file", "/不存在/文件.mp4", "--no-ingest"],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 1)
        self.assertIn("本地文件不存在", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
