import type { PageLoad } from './$types';
import { api } from '$lib/api';
import type { SystemLogsData } from '$lib/types';

export const load: PageLoad = async ({ url }) => {
	const qs = new URLSearchParams();
	for (const key of ['level', 'source', 'page', 'limit']) {
		const v = url.searchParams.get(key);
		if (v) qs.set(key, v);
	}
	const s = qs.toString();
	return { logs: await api<SystemLogsData>(`/system/logs${s ? `?${s}` : ''}`) };
};
