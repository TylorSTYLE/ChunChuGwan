#!/bin/sh
# 바인드 마운트된 아카이브 디렉토리 소유자를 보정한 뒤 비루트(wccg)로 강등해 실행한다.
#
# 호스트의 ./archive 가 없으면 docker 데몬이 root 소유로 만들고, 있더라도
# 컨테이너 사용자(uid 1000)와 소유자가 다를 수 있다. 그 상태로는 sqlite 가
# "unable to open database file" 로 죽으므로, root 로 시작해 소유자만 맞추고
# 즉시 gosu 로 wccg 가 되어 본 명령을 실행한다 (chromium 샌드박스 유지).
set -e

if [ "$(id -u)" = "0" ]; then
    archive_root="${WCCG_ROOT:-/data/archive}"
    mkdir -p "$archive_root"
    # 소유자가 다른 항목만 보정 — 정상 상태에서는 스캔만 하고 끝난다
    find "$archive_root" ! -user wccg -exec chown wccg:wccg {} +
    export HOME=/home/wccg
    exec gosu wccg wccg "$@"
fi

# compose 의 user: 오버라이드 등 이미 비루트면 보정 없이 그대로 실행
exec wccg "$@"
