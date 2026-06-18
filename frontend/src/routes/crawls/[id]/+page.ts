import type { PageLoad } from './$types';
import { api } from '$lib/api';
import type { CrawlDetail } from '$lib/types';

export const load: PageLoad = async ({ params, url }) => {
	const status = url.searchParams.get('status') ?? '';
	const qs = status ? `?status=${encodeURIComponent(status)}` : '';
	const merged = url.searchParams.get('merged') === '1';
	return { detail: await api<CrawlDetail>(`/crawls/${params.id}${qs}`), merged };
};
