import type { PageLoad } from './$types';
import { api } from '$lib/api';
import type { SnapshotMeta } from '$lib/types';

export const load: PageLoad = async ({ params }) => {
	const meta = await api<SnapshotMeta>(`/snapshots/${params.snapId}`);
	return { meta };
};
