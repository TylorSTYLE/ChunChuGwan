import type { PageLoad } from './$types';
import { api } from '$lib/api';
import type { TrashData } from '$lib/types';

export const load: PageLoad = async () => {
	return { trash: await api<TrashData>('/trash') };
};
