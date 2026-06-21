import type { PageLoad } from './$types';
import { api } from '$lib/api';
import type { SearchData } from '$lib/types';

export const load: PageLoad = async ({ url }) => {
	const qs = new URLSearchParams();
	const q = url.searchParams.get('q') ?? '';
	const domain = url.searchParams.get('domain') ?? '';
	const latest = url.searchParams.get('latest') ?? '';
	const page = url.searchParams.get('page') ?? '1';
	if (q) qs.set('q', q);
	if (domain) qs.set('domain', domain);
	if (latest) qs.set('latest', '1');
	if (page !== '1') qs.set('page', page);
	const s = qs.toString();
	return { search: await api<SearchData>(`/search${s ? `?${s}` : ''}`) };
};
