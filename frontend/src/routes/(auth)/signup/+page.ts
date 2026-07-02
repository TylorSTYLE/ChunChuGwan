import type { PageLoad } from './$types';
import { api } from '$lib/api';
import type { AuthConfig } from '$lib/types';

// 가입 화면 — signup_enabled 를 공개 config 로 받아, 꺼져 있으면 폼 대신 안내를 보인다
// (제출 시점 403 이 아니라 진입 시 즉시 알림). 미인증 호출이라 401 처리 안 함.
export const load: PageLoad = async () => {
	return { config: await api<AuthConfig>('/auth/config', { redirectOn401: false }) };
};
