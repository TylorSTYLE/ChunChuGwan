import type { PageLoad } from './$types';
import { api } from '$lib/api';
import type { AuditLogsData } from '$lib/types';

export const load: PageLoad = async ({ url }) => {
	const qs = new URLSearchParams();
	for (const key of ['action', 'actor', 'page', 'limit']) {
		const v = url.searchParams.get(key);
		if (v) qs.set(key, v);
	}
	const s = qs.toString();
	return { audit: await api<AuditLogsData>(`/audit${s ? `?${s}` : ''}`) };
};
