#!/bin/bash
# 用 dufs 发布 /srv/dufs（IPTV 订阅在 /iptv/iptv_collection.m3u）
cd "$(dirname "$0")"
exec "$HOME/bin/dufs" /srv/dufs -b 0.0.0.0 -p 8080 --allow-symlink
