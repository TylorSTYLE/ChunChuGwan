import type { PageLoad } from './$types';
import { api } from '$lib/api';
import type { CredentialsData } from '$lib/types';

export const load: PageLoad = async ({ params }) => {
	return { data: await api<CredentialsData>(`/sites/${params.id}/credentials`) };
};
