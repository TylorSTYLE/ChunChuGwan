import type { LayoutLoad } from './$types';
import { api } from '$lib/api';
import { setTimezone } from '$lib/format';
import type { Me } from '$lib/types';

// 전역 SPA — SSR·프리렌더 끔(정적 셸 + 클라이언트 라우팅, adapter-static fallback).
export const ssr = false;
export const prerender = false;

export const load: LayoutLoad = async () => {
	const me = await api<Me>('/me');
	setTimezone(me.timezone);
	return { me };
};
