import type { PageLoad } from './$types';
import { api } from '$lib/api';
import type { SitesData } from '$lib/types';

export const load: PageLoad = async ({ url }) => {
	const qs = new URLSearchParams();
	for (const key of ['q', 'page', 'limit']) {
		const v = url.searchParams.get(key);
		if (v) qs.set(key, v);
	}
	const s = qs.toString();
	return { sites: await api<SitesData>(`/sites${s ? `?${s}` : ''}`) };
};
