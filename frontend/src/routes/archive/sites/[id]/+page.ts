import type { PageLoad } from './$types';
import { api } from '$lib/api';
import type { SiteDetail } from '$lib/types';

export const load: PageLoad = async ({ params, url }) => {
	// 페이지·회차·실패 목록은 각각 페이징한다 — 6개 파라미터를 URL 에서 그대로 전달.
	const qs = new URLSearchParams();
	for (const key of [
		'page',
		'per_page',
		'crawls_page',
		'crawls_per_page',
		'failed_page',
		'failed_per_page'
	]) {
		const v = url.searchParams.get(key);
		if (v) qs.set(key, v);
	}
	const s = qs.toString();
	const site = await api<SiteDetail>(`/sites/${params.id}${s ? `?${s}` : ''}`);
	return { site };
};
