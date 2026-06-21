import type { PageLoad } from './$types';
import { api } from '$lib/api';
import type { SystemUsersData } from '$lib/types';

export const load: PageLoad = async ({ url }) => {
	const qs = new URLSearchParams();
	for (const key of ['page', 'limit']) {
		const v = url.searchParams.get(key);
		if (v) qs.set(key, v);
	}
	const s = qs.toString();
	return { data: await api<SystemUsersData>(`/system/users${s ? `?${s}` : ''}`) };
};
