#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""直播源检测：两段式预检 + 跨轮失败退避 + 画质提取 + 只对候选做静态画面检测"""

import json
import logging
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from tqdm import tqdm

logger = logging.getLogger("IPTV-Checker")


class IPTVSourceChecker:
    def __init__(self, config, state=None, round_no=0):
        self.config = config
        self.state = state if state is not None else {}
        self.round = round_no
        self.ffmpeg = config.get("ffmpeg_bin", "ffmpeg")
        self.ffprobe = config.get("ffprobe_bin", "ffprobe")
        self.probe_timeout = config.get("probe_timeout", 3)
        self.check_timeout = config.get("check_timeout", 4)
        self.probe_workers = config.get("probe_workers", 64)
        self.workers = config.get("max_workers", 12)
        self.top_n = config.get("static_check_top_n", 3)
        self.max_gap = config.get("retest_backoff_max", 8)
        self.force_days = config.get("force_retest_days", 7)

    # ---------------- 退避策略 ----------------
    def _should_skip(self, url, now):
        st = self.state.get(url)
        if not st:
            return False
        if st.get("ok"):
            return False                      # 上轮成功，必须每轮复测
        if st.get("reason") == "timeout" and self.config.get("timeout_always_retest", True):
            return False                      # 超时类多为抖动，不降频
        fails = max(int(st.get("fails", 1)), 1)
        gap = min(2 ** (fails - 1), self.max_gap)
        if self.round - int(st.get("round", 0)) >= gap:
            return False                      # 到期复测
        # 兜底：太久没测过就强制测一次
        return (now - float(st.get("ts", 0))) < self.force_days * 86400

    def _remember(self, url, ok, reason, now):
        st = self.state.get(url, {})
        st["round"] = self.round
        st["ts"] = now
        if ok:
            st.update({"ok": 1, "fails": 0, "reason": ""})
        else:
            st["ok"] = 0
            st["fails"] = int(st.get("fails", 0)) + 1
            st["reason"] = reason
        self.state[url] = st

    # ---------------- 两段式检测 ----------------
    def _probe(self, url):
        """第一段：HTTP 预检，只取首包，顺带拿 playlist 文本"""
        try:
            with requests.get(
                url, stream=True, timeout=self.probe_timeout,
                headers={"User-Agent": self.config.get("user_agent", "Mozilla/5.0")},
                allow_redirects=True,
            ) as r:
                if r.status_code not in (200, 206):
                    return False, "", f"http_{r.status_code}"
                head = b""
                for chunk in r.iter_content(2048):
                    head += chunk
                    if len(head) >= 8192:
                        break
                return True, head.decode("utf-8", "ignore"), ""
        except requests.Timeout:
            return False, "", "timeout"
        except Exception:
            return False, "", "probe_error"

    @staticmethod
    def _quality_from_playlist(text):
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

    def _ffprobe(self, url, quality):
        """第二段：ffprobe 确认有视频流；超时重试一次"""
        for attempt in (0, 1):
            timeout = self.check_timeout * (1.5 if attempt else 1)
            try:
                start = time.time()
                r = subprocess.run(
                    [self.ffprobe, "-v", "quiet", "-print_format", "json", "-show_streams", "-i", url],
                    capture_output=True, timeout=timeout)
                latency = time.time() - start
                if r.returncode != 0:
                    return False, float("inf"), {}, "ffprobe_fail"
                info = json.loads(r.stdout.decode("utf-8", "ignore") or "{}")
                streams = info.get("streams", [])
                if not any(s.get("codec_type") == "video" for s in streams):
                    return False, float("inf"), {}, "no_video"
                if not quality:
                    h = br = 0
                    for s in streams:
                        if s.get("codec_type") != "video":
                            continue
                        h = max(h, int(s.get("height") or 0))
                        try:
                            br = max(br, int(s.get("bit_rate") or 0))
                        except (TypeError, ValueError):
                            pass
                    quality = {"height": h, "bitrate": br}
                return True, latency, quality, ""
            except subprocess.TimeoutExpired:
                if attempt:
                    return False, float("inf"), {}, "timeout"
            except Exception:
                return False, float("inf"), {}, "ffprobe_error"
        return False, float("inf"), {}, "timeout"

    def _is_static(self, url):
        try:
            r = subprocess.run(
                [self.ffmpeg, "-i", url, "-t", "6", "-vf", "freezedetect=n=0.001:d=4",
                 "-an", "-f", "null", "-"],
                capture_output=True, timeout=20, text=True)
            return "freeze_start" in (r.stderr or "")
        except Exception:
            return False

    def _rank_key(self, item):
        lat, q = item["latency"], item.get("quality") or {}
        thr = self.config.get("latency_threshold", 3.0)
        return (0 if lat <= thr else 1, -int(q.get("height", 0)), -int(q.get("bitrate", 0)), lat)

    def check(self, channels):
        now = time.time()
        tasks, skipped = [], 0
        for channel_id, (info, urls) in channels.items():
            for url in urls:
                if self._should_skip(url, now):
                    skipped += 1
                    continue
                tasks.append((channel_id, info, url))
        logger.info(f"共 {sum(len(u) for _, u in channels.values())} 个源，"
                    f"本轮检测 {len(tasks)} 个，退避跳过 {skipped} 个")

        results = {}
        by_url = {}          # url -> {"info":..., "quality":..., "latency":...}
        valid = {}           # url -> (valid, latency, quality, reason)

        # --- 第一段：HTTP 预检（高并发）---
        def probe(task):
            url = task[2]
            ok, text, reason = self._probe(url)
            return task, ok, text, reason

        with ThreadPoolExecutor(max_workers=self.probe_workers) as ex:
            for task, ok, text, reason in tqdm(ex.map(probe, tasks), total=len(tasks), desc="预检"):
                url = task[2]
                if not ok:
                    valid[url] = (False, float("inf"), {}, reason)
                    continue
                by_url[url] = {"quality": self._quality_from_playlist(text)}

        # --- 第二段：ffprobe ---
        def probe2(url):
            ok, lat, q, reason = self._ffprobe(url, by_url[url]["quality"])
            return url, ok, lat, q, reason

        stage2 = list(by_url.keys())
        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            for url, ok, lat, q, reason in tqdm(ex.map(probe2, stage2), total=len(stage2), desc="探测"):
                valid[url] = (ok, lat, q, reason)

        # --- 第三段：每个频道只对前 N 名做静态画面检测 ---
        cand = {}
        for channel_id, info, url in tasks:
            ok, lat, q, reason = valid.get(url, (False, float("inf"), {}, "skipped"))
            if not ok:
                continue
            cand.setdefault(channel_id, []).append({"url": url, "latency": lat, "quality": q, "info": info})
        freeze_list = []
        for channel_id, items in cand.items():
            items.sort(key=self._rank_key)
            for it in items[:self.top_n]:
                freeze_list.append((channel_id, it))

        def freeze(pair):
            channel_id, it = pair
            return pair, self._is_static(it["url"])

        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            for (channel_id, it), is_static in tqdm(ex.map(freeze, freeze_list),
                                                    total=len(freeze_list), desc="静态检测"):
                if is_static:
                    valid[it["url"]] = (False, float("inf"), {}, "static")

        # --- 汇总 + 记录状态 ---
        for channel_id, info, url in tasks:
            ok, lat, q, reason = valid.get(url, (False, float("inf"), {}, "skipped"))
            results.setdefault(channel_id, {"info": info, "sources": []})
            results[channel_id]["sources"].append((url, ok, lat, q))
            self._remember(url, ok, reason, now)

        n_valid = sum(1 for s in results.values() for x in s["sources"] if x[1])
        logger.info(f"检测完成：{n_valid}/{len(tasks)} 个源有效（跳过 {skipped} 个未测）")
        return results
