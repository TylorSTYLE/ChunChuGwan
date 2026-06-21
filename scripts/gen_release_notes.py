#!/usr/bin/env python3
"""릴리스 노트 번들 생성 — GitHub Release 본문을 표시용 JSON 으로 변환·병합.

릴리스 워크플로(`release.yml`)에서 호출한다. 해당 버전의 릴리즈 노트(markdown)를
stdin 으로 받아 `release_notes.parse_github_notes` 로 변환(수정자 `@user`·원본 링크
제거, PR 번호/URL 만 유지)한 뒤 `chunchugwan/web/release_notes.json` 의 그 버전 키에
써넣는다(기존 버전은 보존, 같은 키는 갱신). 제3자 의존성 없이 표준 라이브러리만 쓴다.

사용:
  gh api repos/$REPO/releases/generate-notes -f tag_name=v$VER --jq .body \\
    | python3 scripts/gen_release_notes.py $VER
  # 또는 이미 게시된 릴리스에서:
  gh release view v$VER --json body -q .body | python3 scripts/gen_release_notes.py $VER
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chunchugwan.web.release_notes import JSON_PATH, parse_github_notes  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: gen_release_notes.py <version>  (릴리스 본문은 stdin)", file=sys.stderr)
        return 2
    version = argv[1].lstrip("v").split("+", 1)[0]
    items = parse_github_notes(sys.stdin.read())

    data: dict = {}
    if JSON_PATH.exists():
        try:
            data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
        except ValueError:
            data = {}
    data[version] = {"items": items}
    JSON_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"release_notes.json: {version} 항목 {len(items)}개 기록 → {JSON_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
