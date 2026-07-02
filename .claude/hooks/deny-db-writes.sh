#!/usr/bin/env bash
# db-analyst 전용 PreToolUse 훅 — Bash 명령의 DB 쓰기 구문 차단(표준 §13.7 필수 동반 훅).
# 프롬프트 지시(1차 방어)와의 이중 구조 — 기계적 차단이 2차 방어선이다.
INPUT=$(cat)
CMD=$(echo "$INPUT" | jq -r '.tool_input.command // empty')

# PRAGMA는 여기서 제외한다 — table_info/index_list 등 읽기 전용 스키마 조회가
# db-analyst의 핵심 절차(스키마·인덱스 파악)라 블랭킷 차단하면 정상 사용을 막는다.
if echo "$CMD" | grep -qiE '\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|TRUNCATE|GRANT|REVOKE|VACUUM|REINDEX)\b'; then
  echo "차단: db-analyst는 읽기 전용이다. DB 쓰기/구조변경 구문이 감지되었다: $CMD" >&2
  exit 2
fi

exit 0
