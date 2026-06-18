import type { LayoutLoad } from './$types';
import { redirect } from '@sveltejs/kit';
import { base } from '$app/paths';

// 미인증·특수(setup·pending) 화면 그룹. 활성 로그인 사용자는 현황으로 보낸다.
// 단, 승인 대기(pending) 계정은 이 그룹의 안내 화면에 머물러야 하므로 예외.
export const load: LayoutLoad = async ({ parent }) => {
	const { me } = await parent();
	if (me && me.user?.role !== 'pending') redirect(302, `${base}/`);
	return {};
};
