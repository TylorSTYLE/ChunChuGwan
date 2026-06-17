import type { PageLoad } from './$types';
import { api } from '$lib/api';
import type { SiteDetail } from '$lib/types';

export const load: PageLoad = async ({ params, url }) => {
	const page = url.searchParams.get('page') ?? '1';
	const site = await api<SiteDetail>(`/sites/${params.id}?page=${page}`);
	return { site };
};
