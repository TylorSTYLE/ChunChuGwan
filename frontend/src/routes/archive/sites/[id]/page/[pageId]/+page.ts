import type { PageLoad } from './$types';
import { api } from '$lib/api';
import type { PageTimeline } from '$lib/types';

export const load: PageLoad = async ({ params, url }) => {
	const qs = new URLSearchParams();
	for (const key of ['page', 'limit']) {
		const v = url.searchParams.get(key);
		if (v) qs.set(key, v);
	}
	const s = qs.toString();
	const tl = await api<PageTimeline>(`/pages/${params.pageId}${s ? `?${s}` : ''}`);
	return { tl };
};
