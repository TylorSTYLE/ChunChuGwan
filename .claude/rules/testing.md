---
description: 테스트 컨벤션 — 기능마다 테스트 추가·네트워크 의존은 fixture HTML. tests/ 를 만질 때.
paths:
  - "tests/**"
---

# 테스트

- 새 기능 = 해당 테스트 추가. 네트워크 의존 테스트는 로컬 fixture HTML 사용.
- 실행: `uv run pytest`.
- UI 문자열을 추가하면 en 카탈로그도 채울 것 — 템플릿 리터럴 키 누락은
  `tests/test_i18n.py` 가 검사한다 (i18n 규칙은 `.claude/rules/dashboard.md`).
