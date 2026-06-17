import type { PageLoad } from './$types';
import { api } from '$lib/api';
import type { PersonalApiKeysData } from '$lib/types';

export const load: PageLoad = async () => {
	return { data: await api<PersonalApiKeysData>('/settings/api-keys') };
};
