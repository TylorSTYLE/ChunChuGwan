import type { PageLoad } from './$types';
import { api } from '$lib/api';
import type { MyArchivesData } from '$lib/types';

export const load: PageLoad = async ({ url }) => {
	const qs = new URLSearchParams();
	for (const key of ['status', 'page', 'limit']) {
		const v = url.searchParams.get(key);
		if (v) qs.set(key, v);
	}
	const q = qs.toString();
	return { data: await api<MyArchivesData>('/settings/archives' + (q ? `?${q}` : '')) };
};
