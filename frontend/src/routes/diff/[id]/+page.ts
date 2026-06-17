import type { PageLoad } from './$types';
import { api } from '$lib/api';
import type { DiffData } from '$lib/types';

export const load: PageLoad = async ({ params, url }) => {
	const qs = new URLSearchParams();
	const from = url.searchParams.get('from');
	const to = url.searchParams.get('to');
	if (from) qs.set('from', from);
	if (to) qs.set('to', to);
	const q = qs.toString();
	const diff = await api<DiffData>(`/diff/${params.id}${q ? `?${q}` : ''}`);
	return { diff };
};
