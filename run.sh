#!/bin/bash
# 本地运行包装：清缓存 + 轮转日志 + 后台跑
set -uo pipefail
cd "$(dirname "$0")"
export PATH="$HOME/bin:$PWD/venv/bin:$PATH"
export TZ=Asia/Shanghai
mkdir -p data/output data/sources logs
find data/sources -type f -mtime +3 -delete 2>/dev/null
echo "=== start $(date '+%F %T') ===" >> logs/run.log
( flock -n 9 || { echo "another instance running, skip"; exit 0; }
  python3 main.py --no-epg >> logs/run.log 2>&1
  rc=$?
  echo "=== exit $rc at $(date '+%F %T') ===" >> logs/run.log
) 9> logs/.lock
