import type { PageLoad } from './$types';
import { api } from '$lib/api';
import type { DocumentsData } from '$lib/types';

export const load: PageLoad = async ({ url }) => {
	// page 를 양의 정수로 검증한다 — 문자열 보간이라 비숫자(?page=abc)는 422 에러 페이지,
	// ?page=1%26foo%3Dbar 류는 API 에 파라미터가 재주입될 수 있다.
	const raw = url.searchParams.get('page');
	const n = raw ? parseInt(raw, 10) : 1;
	const page = Number.isFinite(n) && n > 0 ? n : 1;
	return { docs: await api<DocumentsData>(`/documents?page=${page}`) };
};
