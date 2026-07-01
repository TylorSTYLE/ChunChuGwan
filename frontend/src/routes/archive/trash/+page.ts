import type { PageLoad } from './$types';
import { api } from '$lib/api';
import type { TrashData } from '$lib/types';

export const load: PageLoad = async ({ url }) => {
	const qs = new URLSearchParams();
	// page·limit 은 양의 정수만 통과시킨다 — 비숫자(?page=abc)를 그대로 넘기면 서버가 422 를
	// 내고 목록 대신 에러 페이지가 뜬다(잘못된 값은 무시 → 서버 기본값).
	for (const key of ['page', 'limit']) {
		const v = url.searchParams.get(key);
		if (v) {
			const n = parseInt(v, 10);
			if (Number.isFinite(n) && n > 0) qs.set(key, String(n));
		}
	}
	const s = qs.toString();
	return { trash: await api<TrashData>(`/trash${s ? `?${s}` : ''}`) };
};
