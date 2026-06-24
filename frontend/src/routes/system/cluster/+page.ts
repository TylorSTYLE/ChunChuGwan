import type { PageLoad } from './$types';
import { api } from '$lib/api';
import type { ClusterData } from '$lib/types';

export const load: PageLoad = async () => {
	return { data: await api<ClusterData>('/system/cluster') };
};
