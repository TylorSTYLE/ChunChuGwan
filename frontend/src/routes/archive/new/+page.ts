import type { PageLoad } from './$types';
import { api } from '$lib/api';

type Tag = { id: string; name: string; description: string | null };

export const load: PageLoad = async ({ parent }) => {
	const { me } = await parent();
	const canManageCred = !!me?.flags.can_manage_credentials;
	// 로컬 네트워크 태그 — 사설 IP 주소 아카이빙 시 선택용. 권한·오류 시 빈 목록.
	let networkTags: Tag[] = [];
	try {
		networkTags = (await api<{ network_tags: Tag[] }>('/network-tags')).network_tags;
	} catch {
		networkTags = [];
	}
	return { networkTags, canManageCred };
};
