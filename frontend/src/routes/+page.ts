import type { PageLoad } from './$types';
import { api } from '$lib/api';
import type { Dashboard } from '$lib/types';

export const load: PageLoad = async () => {
	const dashboard = await api<Dashboard>('/dashboard');
	return { dashboard };
};
