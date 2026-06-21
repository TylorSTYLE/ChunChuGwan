import { base } from '$app/paths';

/** 아카이브 자원 URL — 라우트는 사이트→페이지→스냅샷 계층으로 중첩한다.
 *  site_id 를 모르는 경우(일부 목록) 0 으로 떨어뜨려도 페이지·스냅샷은
 *  전역 유일 id 로 로드되므로 동작한다. */
export function pagePath(
	siteId: number | null | undefined,
	pageId: number | null | undefined
): string {
	return `${base}/archive/sites/${siteId ?? 0}/page/${pageId ?? 0}`;
}

export function snapPath(
	siteId: number | null | undefined,
	pageId: number | null | undefined,
	snapId: number
): string {
	return `${base}/archive/sites/${siteId ?? 0}/page/${pageId ?? 0}/snapshot/${snapId}`;
}
