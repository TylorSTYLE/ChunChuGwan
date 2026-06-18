/** `/api/web` 세션 인증 JSON API 클라이언트.
 *
 * - same-origin 쿠키로 세션 인증(FastAPI 가 SPA 를 같은 출처로 서빙).
 * - 변경 요청에 `X-Requested-With` 를 실어 CSRF 방어를 보강한다
 *   (서버는 Origin 검사도 한다 — auth_gate).
 * - 401 은 세션 만료/미인증 — 공존 기간에는 기존 SSR 로그인(/login)으로 보낸다.
 *   빅뱅 컷오버 후에는 SPA 의 /login 라우트로 바꾼다.
 */

export class ApiError extends Error {
	constructor(
		public status: number,
		message: string
	) {
		super(message);
		this.name = 'ApiError';
	}
}

export async function api<T = unknown>(
	path: string,
	opts: RequestInit & { redirectOn401?: boolean } = {}
): Promise<T> {
	// 미인증 흐름(로그인·me 프로브)은 redirectOn401:false 로 401 을 직접 처리한다 —
	// 기본값은 기존 SSR 로그인으로 보내는 공존 동작을 유지한다(컷오버 시 SPA /login 으로).
	const { redirectOn401 = true, ...init } = opts;
	const res = await fetch(`/api/web${path}`, {
		credentials: 'same-origin',
		headers: {
			'X-Requested-With': 'fetch',
			// 문자열 본문만 JSON — FormData(멀티파트 HAR 업로드)는 브라우저가
			// 경계 포함 Content-Type 을 직접 설정하게 둔다.
			...(typeof init.body === 'string' ? { 'Content-Type': 'application/json' } : {}),
			...(init.headers ?? {})
		},
		...init
	});
	if (res.status === 401) {
		if (redirectOn401 && typeof window !== 'undefined') {
			const next = encodeURIComponent(window.location.pathname + window.location.search);
			window.location.href = `/login?next=${next}`;
		}
		throw new ApiError(401, '인증이 필요합니다');
	}
	if (!res.ok) {
		let detail = res.statusText;
		try {
			const body = await res.json();
			detail = body.detail ?? detail;
		} catch {
			/* JSON 아님 — statusText 유지 */
		}
		throw new ApiError(res.status, detail);
	}
	return res.json() as Promise<T>;
}
