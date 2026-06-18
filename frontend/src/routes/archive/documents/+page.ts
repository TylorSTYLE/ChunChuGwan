import type { PageLoad } from './$types';
import { api } from '$lib/api';
import type { DocumentsData } from '$lib/types';

export const load: PageLoad = async ({ url }) => {
	const page = url.searchParams.get('page') ?? '1';
	return { docs: await api<DocumentsData>(`/documents?page=${page}`) };
};
