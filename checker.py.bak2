#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import re
import logging
import subprocess
import json
import requests
from concurrent.futures import ThreadPoolExecutor
from tqdm import tqdm

logger = logging.getLogger("IPTV-Checker")


class IPTVSourceChecker:
    def __init__(self, config):
        self.config = config
        self.results = {}  # {频道ID: {"info": info, "sources": [(URL, 是否有效, 延迟, 画质)]}}

    # ---------- 第一段：轻量 HTTP 预检 + 顺手读 master playlist 的画质 ----------
    def _probe(self, url):
        """只取首包；返回 (是否可用, 头部文本)"""
        try:
            with requests.get(
                url,
                stream=True,
                timeout=self.config.get("probe_timeout", 3),
                headers={"User-Agent": self.config.get("user_agent", "Mozilla/5.0")},
                allow_redirects=True,
            ) as r:
                if r.status_code not in (200, 206):
                    return False, ""
                head = b""
                for chunk in r.iter_content(2048):
                    head += chunk
                    if len(head) >= 8192:
                        break
                return True, head.decode("utf-8", "ignore")
        except Exception:
            return False, ""

    @staticmethod
    def _quality_from_playlist(text):
        """从 m3u8 master playlist 提取最高档的 RESOLUTION/BANDWIDTH"""
        if "#EXT-X-STREAM-INF" not in text:
            return {}
        best = {"height": 0, "bitrate": 0}
        for line in text.splitlines():
            if not line.startswith("#EXT-X-STREAM-INF"):
                continue
            bw = re.search(r"BANDWIDTH=(\d+)", line)
            rs = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
            h = int(rs.group(2)) if rs else 0
            b = int(bw.group(1)) if bw else 0
            if (h, b) > (best["height"], best["bitrate"]):
                best = {"height": h, "bitrate": b}
        return best

    def check(self, channels):
        logger.info(f"开始检查 {len(channels)} 个频道的直播源...")
        check_tasks = []
        for channel_id, (info, urls) in channels.items():
            for url in urls:
                check_tasks.append((channel_id, info, url))

        logger.info(f"共 {len(check_tasks)} 个直播源需要检查")
        with ThreadPoolExecutor(max_workers=self.config["max_workers"]) as executor:
            futures = {executor.submit(self._check_source, task[2]): task for task in check_tasks}
            with tqdm(total=len(futures), desc="检查直播源") as pbar:
                for future in futures:
                    channel_id, info, url = futures[future]
                    try:
                        is_valid, latency, quality = future.result()
                        if channel_id not in self.results:
                            self.results[channel_id] = {"info": info, "sources": []}
                        self.results[channel_id]["sources"].append((url, is_valid, latency, quality))
                    except Exception as e:
                        logger.error(f"检查任务失败: {channel_id}, {url}, 错误: {str(e)}")
                        if channel_id not in self.results:
                            self.results[channel_id] = {"info": info, "sources": []}
                        self.results[channel_id]["sources"].append((url, False, float("inf"), {}))
                    finally:
                        pbar.update(1)

        total_channels = len(self.results)
        valid_channels = sum(1 for r in self.results.values() if any(s[1] for s in r["sources"]))
        total_sources = sum(len(r["sources"]) for r in self.results.values())
        valid_sources = sum(sum(1 for s in r["sources"] if s[1]) for r in self.results.values())
        logger.info("直播源检查完成")
        logger.info(f"频道统计: {valid_channels}/{total_channels} 个频道有效")
        logger.info(f"直播源统计: {valid_sources}/{total_sources} 个直播源有效")
        return self.results

    def _check_source(self, url):
        """返回 (是否有效, 延迟, 画质dict)"""
        try:
            # 第一段：HTTP 预检，不通就不浪费 ffprobe
            ok, head = self._probe(url)
            if not ok:
                return False, float("inf"), {}
            quality = self._quality_from_playlist(head)

            start_time = time.time()
            cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", "-i", url]
            result = subprocess.run(cmd, capture_output=True, timeout=self.config["check_timeout"])
            latency = time.time() - start_time

            if result.returncode != 0:
                return False, float("inf"), {}

            try:
                output = result.stdout.decode("utf-8", errors="ignore")
                stream_info = json.loads(output) if output else {}
            except json.JSONDecodeError:
                return False, float("inf"), {}

            streams = stream_info.get("streams", [])
            has_video = any(s.get("codec_type") == "video" for s in streams)
            if not has_video:
                return False, float("inf"), {}

            # playlist 里没写画质（普通 media playlist）时，用 ffprobe 读到的补上
            if not quality:
                h = 0
                br = 0
                for s in streams:
                    if s.get("codec_type") != "video":
                        continue
                    h = max(h, int(s.get("height") or 0))
                    try:
                        br = max(br, int(s.get("bit_rate") or 0))
                    except (TypeError, ValueError):
                        pass
                quality = {"height": h, "bitrate": br}

            if self._is_static_stream(url):
                return False, float("inf"), {}
            return True, latency, quality

        except subprocess.TimeoutExpired:
            return False, float("inf"), {}
        except Exception as e:
            logger.debug(f"检查出错: {url}, 错误: {str(e)}")
            return False, float("inf"), {}

    def _is_static_stream(self, url):
        """取 6 秒视频，画面冻结超 4 秒判定为静态"""
        try:
            cmd = ["ffmpeg", "-i", url, "-t", "6", "-vf", "freezedetect=n=0.001:d=4",
                   "-an", "-f", "null", "-"]
            result = subprocess.run(cmd, capture_output=True, timeout=20, text=True)
            return "freeze_start" in result.stderr
        except subprocess.TimeoutExpired:
            return False
        except Exception:
            return False
