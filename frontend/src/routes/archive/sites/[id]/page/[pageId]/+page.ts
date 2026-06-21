import type { PageLoad } from './$types';
import { api } from '$lib/api';
import type { PageTimeline } from '$lib/types';

export const load: PageLoad = async ({ params }) => {
	const tl = await api<PageTimeline>(`/pages/${params.pageId}`);
	return { tl };
};
