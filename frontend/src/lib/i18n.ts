/** 번역 — 한국어 원문을 메시지 키로 쓰는 gettext 방식(기존 web/i18n.py 와 동일 모델).
 *
 * vertical slice 단계에서는 패스스루(한국어 원문 반환)다. ko/en 카탈로그 추출은
 * 후속 작업에서 빌드 타임에 web/i18n.py → JSON 으로 생성해 `setCatalog` 로 주입한다.
 * (CLAUDE.md/dashboard 규칙: en 카탈로그 채우기 + test_i18n 키 검증 갱신)
 */

let catalog: Record<string, string> = {};

export function setCatalog(c: Record<string, string>): void {
	catalog = c;
}

export function t(msg: string): string {
	return catalog[msg] ?? msg;
}
