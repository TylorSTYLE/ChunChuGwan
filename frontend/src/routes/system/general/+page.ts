import type { PageLoad } from './$types';
import { api } from '$lib/api';
import type { SystemOverview } from '$lib/types';

export const load: PageLoad = async () => {
	return { sys: await api<SystemOverview>('/system') };
};
