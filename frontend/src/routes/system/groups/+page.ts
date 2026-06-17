import type { PageLoad } from './$types';
import { api } from '$lib/api';
import type { SystemGroupsData } from '$lib/types';

export const load: PageLoad = async () => {
	return { data: await api<SystemGroupsData>('/system/groups') };
};
