import type { PageLoad } from './$types';
import { redirect } from '@sveltejs/kit';
import { base } from '$app/paths';
import { api } from '$lib/api';
import type { SetupStatus } from '$lib/types';

// 최초 설정 — 사용자가 이미 있으면(설정 완료) 로그인으로. 진행 중 이전/복구면 상태도 받는다.
export const load: PageLoad = async () => {
	const s = await api<SetupStatus>('/auth/setup', { redirectOn401: false });
	const migrateActive = ['connecting', 'manifest', 'downloading', 'restoring'].includes(
		s.migration.status
	);
	const recoverActive = ['scanning', 'rebuilding'].includes(s.recovery?.status ?? '');
	if (!s.needed && !migrateActive && !recoverActive) redirect(302, `${base}/login`);
	return {
		migration: s.migration,
		tokenRequired: s.token_required,
		kase: s.case,
		flags: {
			has_archive_data: s.has_archive_data ?? false,
			s3_db_backup: s.s3_db_backup ?? false
		},
		recovery: s.recovery ?? null
	};
};
