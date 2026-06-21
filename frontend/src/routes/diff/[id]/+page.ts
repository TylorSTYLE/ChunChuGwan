import type { PageLoad } from './$types';
import { api, ApiError } from '$lib/api';
import type { DiffData } from '$lib/types';

export const load: PageLoad = async ({ params, url }) => {
	const qs = new URLSearchParams();
	const from = url.searchParams.get('from');
	const to = url.searchParams.get('to');
	if (from) qs.set('from', from);
	if (to) qs.set('to', to);
	const q = qs.toString();
	try {
		const diff = await api<DiffData>(`/diff/${params.id}${q ? `?${q}` : ''}`);
		return { diff, unavailable: null as string | null };
	} catch (e) {
		// 스냅샷 1개뿐 등 비교 불가(400)는 에러 페이지 대신 화면 안에서 안내한다.
		// 그 외(404·500 등)는 +error.svelte 가 처리하도록 다시 던진다.
		if (e instanceof ApiError && e.status === 400) {
			return { diff: null as DiffData | null, unavailable: e.message };
		}
		throw e;
	}
};
