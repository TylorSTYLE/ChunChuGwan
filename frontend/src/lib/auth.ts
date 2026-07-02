/** 인증 흐름 화면 전이 — 로그인·2단계·가입 응답의 status 에 따라 다음 SPA 라우트로 이동. */
import { goto } from '$app/navigation';
import { resolve } from '$app/paths';
import type { AuthStatus } from './types';

export async function afterAuth(status: AuthStatus): Promise<void> {
	if (status === 'totp') {
		await goto(resolve('/login/totp'));
		return;
	}
	if (status === 'email_verify') {
		await goto(resolve('/verify-email'));
		return;
	}
	// active — 세션이 섰으니 루트 레이아웃 me 를 다시 불러(invalidateAll) 현황으로 이동한다.
	await goto(resolve('/'), { invalidateAll: true });
}
