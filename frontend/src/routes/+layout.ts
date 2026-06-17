import type { LayoutLoad } from './$types';
import { api } from '$lib/api';
import { setTimezone } from '$lib/format';
import { setCatalog } from '$lib/i18n';
import type { Me } from '$lib/types';

// 전역 SPA — SSR·프리렌더 끔(정적 셸 + 클라이언트 라우팅, adapter-static fallback).
export const ssr = false;
export const prerender = false;

// 카탈로그는 로케일당 1회만 받는다(언어 변경 시 account 가 페이지를 리로드).
let catalogLocale = '';

export const load: LayoutLoad = async () => {
	const me = await api<Me>('/me');
	setTimezone(me.timezone);
	const loc = me.locale && me.locale !== 'ko' ? me.locale : 'ko';
	if (loc !== catalogLocale) {
		try {
			setCatalog(loc === 'ko' ? {} : await api<Record<string, string>>(`/i18n/${loc}`));
			catalogLocale = loc;
		} catch {
			setCatalog({}); // 카탈로그 로드 실패 — 원문(한국어)로 폴백
		}
	}
	return { me };
};
