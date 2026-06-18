import type { PageLoad } from './$types';
import { api } from '$lib/api';
import type { SiteItem } from '$lib/types';

export const load: PageLoad = async () => {
	const { items } = await api<{ items: SiteItem[] }>('/sites');
	return { sites: items };
};
