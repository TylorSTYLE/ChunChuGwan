import type { PageLoad } from './$types';
import { redirect } from '@sveltejs/kit';
import { base } from '$app/paths';

// 승인 대기 안내 — 루트 레이아웃의 me 에서 이메일을 얻는다. 미인증/활성 계정은 부적격.
export const load: PageLoad = async ({ parent }) => {
	const { me } = await parent();
	if (!me) redirect(302, `${base}/login`);
	if (me.user?.role !== 'pending') redirect(302, `${base}/`);
	return { email: me.user?.email ?? '' };
};
