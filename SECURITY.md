# 보안 정책 (Security Policy)

춘추관은 인증·외부 자격증명 대칭 암호화·사설 IP(SSRF) 게이트 등 보안 민감 기능을
다룬다. 취약점을 발견하면 아래 절차로 **비공개** 제보해 주세요.

## 취약점 제보

- **공개 이슈로 올리지 마세요.**
- **GitHub Private Vulnerability Reporting**(권장): 저장소 **Security → Report a
  vulnerability**. (활성화돼 있지 않아 버튼이 보이지 않으면 아래 이메일을 이용하세요.)
- **이메일 폴백**: tylorstyle@gmail.com — 제목에 `[SECURITY]` 를 붙여 주세요.

제보에 다음을 포함하면 확인이 빠릅니다: 영향 범위, 재현 절차, PoC(있으면), 제안 완화책.
가능한 한 빠르게(대개 수일 내) 접수를 회신하고, 수정 후 원치 않으시면 익명으로,
아니면 제보자 크레딧과 함께 공개한다.

## 지원 범위

개인 프로젝트로 최신 릴리스(`main`)와 개발 브랜치(`develop`)를 대상으로 한다.
아키텍처 보안 원칙(인증 데이터 단방향 저장, 대시보드 loopback·`<iframe sandbox>`,
사설 IP·루프백 게이트 등)은 [CLAUDE.md](CLAUDE.md) "아키텍처 원칙" 절과
[docs/](docs/) 각 문서의 "설계 원칙" 절을 참조하세요.
