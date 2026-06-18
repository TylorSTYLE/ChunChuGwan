import type { PageLoad } from './$types';
import { api } from '$lib/api';
import type { SystemApiKeysData } from '$lib/types';

export const load: PageLoad = async () => {
	return { data: await api<SystemApiKeysData>('/system/api-keys') };
};
