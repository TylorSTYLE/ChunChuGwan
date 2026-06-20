import { base } from '$app/paths';

export type FilterVal = string | number | boolean | null | undefined;

/** 필터/페이지 파라미터를 쿼리스트링(`?a=1&b=2` 또는 빈 문자열)으로 만든다.
 *
 * 기본값과 같거나 빈 값(''·null·undefined·false)은 생략한다. filterUrl(라우트 URL)과
 * list.svelte.ts 의 createList(API 경로)가 같은 파라미터 집합을 공유하므로 이 빌더로 통일한다.
 */
export function queryString(
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
	return s ? `?${s}` : '';
}

/** 필터/페이지 파라미터로 라우트 URL(base 접두 + 쿼리스트링)을 만든다.
 *
 * 로그·검색 등 목록 페이지마다 거의 동일하게 반복되던 applyFilter/pageUrl 로직을 공통화한다.
 *
 *   filterUrl('/log/archive', { domain, status, page }, { page: 1 })
 */
export function filterUrl(
	path: string,
	params: Record<string, FilterVal>,
	defaults: Record<string, FilterVal> = {}
): string {
	return `${base}${path}${queryString(params, defaults)}`;
}
