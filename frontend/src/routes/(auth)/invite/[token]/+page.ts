import type { PageLoad } from './$types';
import { api, ApiError } from '$lib/api';

// 초대 수락 화면 — 토큰 유효성·대상 이메일을 미인증 호출로 확인한다(401 처리 안 함).
// 토큰이 무효/만료면 problem 메시지로 안내하고 패스워드 폼은 숨긴다.
export const load: PageLoad = async ({ params }) => {
	try {
		const info = await api<{ email: string }>(
			`/auth/invite/${encodeURIComponent(params.token)}`,
			{ redirectOn401: false }
		);
		return { token: params.token, email: info.email, problem: '' };
	} catch (err) {
		return {
			token: params.token,
			email: '',
			problem: err instanceof ApiError ? err.message : String(err)
		};
	}
};
