#!/usr/bin/env bash
# PostToolUse(Edit|Write) 훅 — 방금 수정된 파일만 스택별 린터로 검사한다(표준 §13.3
# "판단의 기계화" — CI 도구(§8.2 ci.yml)와 동일한 도구를 써서 로컬·CI 기준을 일치시킨다).
# PostToolUse는 이미 끝난 액션을 막을 수 없으므로(차단 불가), 실패 시 exit 0 +
# hookSpecificOutput.additionalContext로 린터 출력 전문을 에이전트에게 돌려준다.
set -u

INPUT=$(cat)
FILE=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')
CWD=$(echo "$INPUT" | jq -r '.cwd // empty')

[ -z "$FILE" ] && exit 0

# 상대경로로 오면 훅 실행 시점의 cwd 기준으로 절대경로화.
if [[ "$FILE" != /* ]]; then
  FILE="$CWD/$FILE"
fi
[ -f "$FILE" ] || exit 0

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
report() {
  # $1 = 린터 원본 출력(여러 줄 가능)
  jq -n --arg ctx "$1" '{
    hookSpecificOutput: {
      hookEventName: "PostToolUse",
      additionalContext: $ctx
    }
  }'
}

case "$FILE" in
  "$REPO_ROOT"/*.py)
    if ! OUT=$(cd "$REPO_ROOT" && uv run ruff check "$FILE" 2>&1); then
      report "ruff check 실패 — 이 파일을 직접 수정해 해소할 것:
$OUT"
    fi
    ;;
  "$REPO_ROOT"/frontend/*.ts|"$REPO_ROOT"/frontend/*.svelte|"$REPO_ROOT"/frontend/*.js)
    REL="${FILE#"$REPO_ROOT"/frontend/}"
    if ! OUT=$(cd "$REPO_ROOT/frontend" && pnpm exec eslint "$REL" 2>&1); then
      report "eslint 실패 — 이 파일을 직접 수정해 해소할 것:
$OUT"
    fi
    ;;
  *)
    exit 0
    ;;
esac

exit 0
