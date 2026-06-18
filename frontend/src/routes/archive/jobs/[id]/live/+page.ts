import type { PageLoad } from './$types';
import { redirect } from '@sveltejs/kit';
import { base } from '$app/paths';
import { api, ApiError } from '$lib/api';
import type { LiveMeta } from '$lib/types';

// 라이브 세션 열기 = 클레임. 이미 끝났거나(404) 만료면 목록으로 돌려보낸다.
export const load: PageLoad = async ({ params }) => {
	try {
		return { meta: await api<LiveMeta>(`/live/${params.id}`) };
	} catch (err) {
		if (err instanceof ApiError && err.status === 404) redirect(303, `${base}/archive/needs-human`);
		throw err;
	}
};
