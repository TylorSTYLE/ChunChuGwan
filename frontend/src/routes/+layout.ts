import type { LayoutLoad } from './$types';
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

export const load: LayoutLoad = async () => {
	// 미인증(AUTH on)이면 /me 가 401 — SSR 로 튕기지 않고 me:null 로 셸만 렌더한다.
	// (auth) 라우트 그룹이 헤더·카탈로그 없이 로그인 화면을 띄운다.
	let me: Me | null;
	try {
		me = await api<Me>('/me', { redirectOn401: false });
	} catch (err) {
		if (err instanceof ApiError && err.status === 401) {
			await applyCatalog('ko'); // 로그인 화면은 한국어 원문(공개 i18n 은 화면별로 받는다)
			return { me: null };
		}
		throw err;
	}
	setTimezone(me.timezone);
	await applyCatalog(me.locale && me.locale !== 'ko' ? me.locale : 'ko');
	return { me };
};
