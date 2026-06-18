import type { LayoutLoad } from './$types';
import { redirect } from '@sveltejs/kit';
import { base } from '$app/paths';

// 미인증 화면 그룹 — 이미 로그인돼 있으면(루트 레이아웃이 me 를 실음) 현황으로 보낸다.
export const load: LayoutLoad = async ({ parent }) => {
	const { me } = await parent();
	if (me) redirect(302, `${base}/`);
	return {};
};
