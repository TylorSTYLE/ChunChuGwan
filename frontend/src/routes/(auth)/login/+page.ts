import type { PageLoad } from './$types';
import { api } from '$lib/api';
import type { AuthConfig } from '$lib/types';

// 로그인 화면 — SSO·가입 노출 여부를 공개 config 로 받는다(미인증 호출이라 401 처리 안 함).
export const load: PageLoad = async () => {
	return { config: await api<AuthConfig>('/auth/config', { redirectOn401: false }) };
};
