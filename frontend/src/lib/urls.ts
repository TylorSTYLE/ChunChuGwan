import { resolve } from '$app/paths';
import type { ResolvedPathname } from '$app/types';

/** 아카이브 자원 URL — 라우트는 사이트→페이지→스냅샷 계층으로 중첩한다.
 *  site_id 를 모르는 경우(일부 목록) 0 으로 떨어뜨려도 페이지·스냅샷은
 *  전역 유일 id 로 로드되므로 동작한다. */
export function pagePath(
	siteId: number | null | undefined,
	pageId: number | null | undefined
): ResolvedPathname {
	return resolve('/archive/sites/[id]/page/[pageId]', {
		id: String(siteId ?? 0),
		pageId: String(pageId ?? 0)
	});
}

export function snapPath(
	siteId: number | null | undefined,
	pageId: number | null | undefined,
	snapId: number
): ResolvedPathname {
	return resolve('/archive/sites/[id]/page/[pageId]/snapshot/[snapId]', {
		id: String(siteId ?? 0),
		pageId: String(pageId ?? 0),
		snapId: String(snapId)
	});
}
