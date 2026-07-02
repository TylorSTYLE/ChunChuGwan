import type { PageLoad } from './$types';
import { api } from '$lib/api';

type Tag = { id: string; name: string; description: string | null };
type CrawlDefaults = { max_pages: number; max_depth: number; delay: number };
type CrawlLimits = { max_pages: number; max_depth: number; max_delay: number };

export const load: PageLoad = async ({ parent }) => {
	const { me } = await parent();
	const canManageCred = !!me?.flags.can_manage_credentials;
	// 로컬 네트워크 태그(사설 IP 선택용) + 사이트 아카이브 기본값·상한. 권한·오류 시 빈 값.
	let networkTags: Tag[];
	let crawlDefaults: CrawlDefaults | null = null;
	let crawlLimits: CrawlLimits | null = null;
	try {
		const r = await api<{
			network_tags: Tag[];
			crawl_defaults: CrawlDefaults;
			crawl_limits: CrawlLimits;
		}>('/network-tags');
		networkTags = r.network_tags;
		crawlDefaults = r.crawl_defaults;
		crawlLimits = r.crawl_limits;
	} catch {
		networkTags = [];
	}
	return { networkTags, canManageCred, crawlDefaults, crawlLimits };
};
