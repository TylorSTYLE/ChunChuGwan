import type { LayoutLoad } from './$types';
import { redirect } from '@sveltejs/kit';
import { base } from '$app/paths';
import { api, ApiError } from '$lib/api';
import { setTimezone } from '$lib/format';
import { setCatalog } from '$lib/i18n';
import type { Me } from '$lib/types';

// 전역 SPA — SSR·프리렌더 끔(정적 셸 + 클라이언트 라우팅, adapter-static fallback).
export const ssr = false;
export const prerender = false;

// 카탈로그는 로케일당 1회만 받는다(언어 변경 시 account 가 페이지를 리로드).
let catalogLocale = '';

async function applyCatalog(loc: string): Promise<void> {
	if (loc === catalogLocale) return;
	try {
		setCatalog(loc === 'ko' ? {} : await api<Record<string, string>>(`/i18n/${loc}`));
		catalogLocale = loc;
	} catch {
		setCatalog({}); // 카탈로그 로드 실패 — 원문(한국어)로 폴백
	}
}

// 미인증으로 머무를 수 있는 화면(로그인·가입·이메일 인증·setup·pending).
// 이 외의 보호된 화면에 미인증으로 들어오면 레이아웃이 로그인으로 보낸다.
const AUTH_ROUTES = ['/login', '/login/totp', '/signup', '/verify-email', '/setup', '/pending'];
// 토큰을 경로에 담는 미인증 화면은 프리픽스로 매칭한다 — 초대 수락(/invite/{token})은
// 아직 계정이 없는 피초대자가 여는 링크라 로그인으로 튕기면 안 된다.
const AUTH_ROUTE_PREFIXES = ['/invite/'];

export const load: LayoutLoad = async ({ url }) => {
	const at = (p: string) => url.pathname === `${base}${p}`;
	const onAuthRoute =
		AUTH_ROUTES.some((r) => at(r)) ||
		AUTH_ROUTE_PREFIXES.some((p) => url.pathname.startsWith(`${base}${p}`));
	// 미인증(AUTH on)이면 /me 가 401 — SSR 로 튕기지 않고 me:null 로 셸만 렌더한다.
	// (auth) 라우트 그룹이 헤더·카탈로그 없이 로그인 화면을 띄운다.
	let me: Me | null;
	try {
		me = await api<Me>('/me', { redirectOn401: false });
	} catch (err) {
		if (err instanceof ApiError && err.status === 401) {
			// 최초 구동(사용자 0명)이면 setup 화면으로 보낸다(부트스트랩).
			let setupNeeded = false;
			try {
				setupNeeded = (
					await api<{ needed: boolean }>('/auth/setup', { redirectOn401: false })
				).needed;
			} catch {
				/* setup 조회 실패(설정 완료 등) — 일반 로그인으로 */
			}
			if (setupNeeded && !at('/setup')) redirect(302, `${base}/setup`);
			// 보호된 화면에 미인증 진입 — 로그인으로(인증 라우트는 그대로 렌더).
			if (!onAuthRoute) redirect(302, `${base}/login`);
			await applyCatalog('ko'); // 로그인 화면은 한국어 원문(공개 i18n 은 화면별로 받는다)
			return { me: null };
		}
		throw err;
	}
	// 승인 대기 계정 — 안내 화면으로(자기 자신 제외).
	if (me.user?.role === 'pending' && !at('/pending')) redirect(302, `${base}/pending`);
	setTimezone(me.timezone);
	await applyCatalog(me.locale && me.locale !== 'ko' ? me.locale : 'ko');
	return { me };
};
