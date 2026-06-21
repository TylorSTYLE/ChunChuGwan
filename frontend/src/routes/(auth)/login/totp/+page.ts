import type { PageLoad } from './$types';
import { redirect } from '@sveltejs/kit';
import { base } from '$app/paths';
import { api, ApiError } from '$lib/api';
import type { TotpStatus } from '$lib/types';

// 2단계 인증 화면 — pending_totp 세션이 없으면(401) 로그인부터 다시.
export const load: PageLoad = async () => {
	try {
		return { status: await api<TotpStatus>('/auth/login/totp', { redirectOn401: false }) };
	} catch (err) {
		if (err instanceof ApiError && err.status === 401) redirect(302, `${base}/login`);
		throw err;
	}
};
