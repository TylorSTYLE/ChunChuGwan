import type { PageLoad } from './$types';
import { api } from '$lib/api';
import type { SchedulesData } from '$lib/types';

export const load: PageLoad = async () => {
	return { sched: await api<SchedulesData>('/schedules') };
};
