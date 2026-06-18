import { base } from '$app/paths';

type FilterVal = string | number | boolean | null | undefined;

/** 필터/페이지 파라미터로 라우트 URL(쿼리스트링 포함)을 만든다.
 *
 * 기본값과 같거나 빈 값(''·null·undefined·false)은 생략한다. 로그·검색 등
 * 목록 페이지마다 거의 동일하게 반복되던 applyFilter/pageUrl 로직을 공통화한다.
 *
 *   filterUrl('/log/archive', { domain, status, page }, { page: 1 })
 */
export function filterUrl(
	path: string,
	params: Record<string, FilterVal>,
	defaults: Record<string, FilterVal> = {}
): string {
	const qs = new URLSearchParams();
	for (const [k, v] of Object.entries(params)) {
		if (v === null || v === undefined || v === '' || v === false) continue;
		if (defaults[k] !== undefined && String(v) === String(defaults[k])) continue;
		qs.set(k, String(v));
	}
	const s = qs.toString();
	return `${base}${path}${s ? `?${s}` : ''}`;
}
