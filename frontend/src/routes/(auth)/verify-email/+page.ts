import type { PageLoad } from './$types';
import { redirect } from '@sveltejs/kit';
import { base } from '$app/paths';
import { api, ApiError } from '$lib/api';
import type { VerifyEmailStatus } from '$lib/types';

// 이메일 본인 인증 화면 — 인증 대상(pending_email_verify 세션 등)이 없으면 로그인으로.
export const load: PageLoad = async () => {
	try {
		return { status: await api<VerifyEmailStatus>('/auth/verify-email/status', { redirectOn401: false }) };
	} catch (err) {
		if (err instanceof ApiError && err.status === 401) redirect(302, `${base}/login`);
		throw err;
	}
};
