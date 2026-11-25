#!/bin/bash
# 本地运行包装：清缓存 + 轮转日志 + 后台跑
set -uo pipefail
cd "$(dirname "$0")"
export PATH="$HOME/bin:$PWD/venv/bin:$PATH"
export TZ=Asia/Shanghai
mkdir -p data/output data/sources logs
rm -rf data/sources/* 2>/dev/null
echo "=== start $(date '+%F %T') ===" >> logs/run.log
( flock -n 9 || { echo "another instance running, skip"; exit 0; }
  python3 main.py --no-epg >> logs/run.log 2>&1
  rc=$?
  # 发布到 dufs（软链，原地截断所以内容自动更新）
  mkdir -p /srv/dufs/iptv 2>/dev/null
  ln -sfn "$PWD/data/output/iptv_collection.m3u" /srv/dufs/iptv/iptv_collection.m3u
  ln -sfn "$PWD/data/output/iptv_collection.m3u" /srv/dufs/iptv_collection.m3u
  echo "=== exit $rc at $(date '+%F %T') ===" >> logs/run.log
) 9> logs/.lock
