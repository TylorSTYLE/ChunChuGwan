import type { PageLoad } from './$types';
import { api } from '$lib/api';
import type { SystemUsersData } from '$lib/types';

export const load: PageLoad = async ({ url }) => {
	const qs = new URLSearchParams();
	// page·limit 은 양의 정수만 통과시킨다 — 비숫자를 넘기면 서버 422 → 에러 페이지.
	for (const key of ['page', 'limit']) {
		const v = url.searchParams.get(key);
		if (v) {
			const n = parseInt(v, 10);
			if (Number.isFinite(n) && n > 0) qs.set(key, String(n));
		}
	}
	const s = qs.toString();
	return { data: await api<SystemUsersData>(`/system/users${s ? `?${s}` : ''}`) };
};
