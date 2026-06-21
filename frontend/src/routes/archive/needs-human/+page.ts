import type { PageLoad } from './$types';
import { api } from '$lib/api';
import type { LiveJob } from '$lib/types';

export const load: PageLoad = async () => {
	return { jobs: (await api<{ jobs: LiveJob[] }>('/live')).jobs };
};
