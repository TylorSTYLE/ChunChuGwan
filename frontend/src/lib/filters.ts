import { resolve } from '$app/paths';
import type { Pathname, ResolvedPathname } from '$app/types';

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

/** 필터/페이지 파라미터로 라우트 URL(resolve() 의 base 접두 + 쿼리스트링)을 만든다.
 *
 * 로그·검색 등 목록 페이지마다 거의 동일하게 반복되던 applyFilter/pageUrl 로직을 공통화한다.
 * path 는 호출부에서 정적 리터럴(예: '/log/archive')뿐 아니라 사이트 상세처럼 파라미터로
 * 조립한 경로(예: `/archive/sites/${id}`)로도 넘어와 컴파일 타임에 Pathname 리터럴로
 * 좁힐 수 없다 — resolve() 의 오버로드는 인자가 구체적 리터럴이어야 어느 라우트 튜플에
 * 매칭할지 고를 수 있어(Pathname 합집합 그대로는 어떤 가지에도 안 붙는다), 여기서는
 * resolve 를 단순 시그니처로 캐스트해 호출한다(런타임 동작은 그대로 base 접두 + 그대로
 * 반환 — resolve_route 는 [id] 같은 대괄호 세그먼트가 없으면 경로를 그대로 통과시킨다).
 * 쿼리스트링을 이어 붙이면 타입이 string 으로 넓어지므로 최종 반환에서 다시
 * ResolvedPathname 으로 좁힌다.
 *
 *   filterUrl('/log/archive', { domain, status, page }, { page: 1 })
 */
const resolvePathname = resolve as (path: Pathname) => ResolvedPathname;

export function filterUrl(
	path: string,
	params: Record<string, FilterVal>,
	defaults: Record<string, FilterVal> = {}
): ResolvedPathname {
	return (resolvePathname(path as Pathname) + queryString(params, defaults)) as ResolvedPathname;
}
