import type { PageLoad } from './$types';
import { redirect } from '@sveltejs/kit';
import { base } from '$app/paths';
import { api } from '$lib/api';
import type { MigrationStatus } from '$lib/types';

// 최초 설정 — 사용자가 이미 있으면(설정 완료) 로그인으로. 진행 중 이전이면 상태도 받는다.
export const load: PageLoad = async () => {
	const s = await api<{ needed: boolean; migration: MigrationStatus; token_required: boolean }>(
		'/auth/setup',
		{ redirectOn401: false }
	);
	const active = ['connecting', 'manifest', 'downloading', 'restoring'].includes(
		s.migration.status
	);
	if (!s.needed && !active) redirect(302, `${base}/login`);
	return { migration: s.migration, tokenRequired: s.token_required };
};
