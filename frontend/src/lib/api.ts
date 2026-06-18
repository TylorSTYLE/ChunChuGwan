/** `/api/web` 세션 인증 JSON API 클라이언트.
 *
 * - same-origin 쿠키로 세션 인증(FastAPI 가 SPA 를 같은 출처로 서빙).
 * - 변경 요청에 `X-Requested-With` 를 실어 CSRF 방어를 보강한다
 *   (서버는 Origin 검사도 한다 — auth_gate).
 * - 401(세션 만료/미인증)은 던지기만 한다. 인증 라우팅은 루트 레이아웃이
 *   단일 권위로 담당한다(매 네비게이션마다 /me 로 재평가 → setup·pending·login).
 *   페이지 로드가 401 을 직접 리다이렉트하면 레이아웃 부트스트랩과 경합하므로
 *   하지 않는다. 액션 호출의 401 은 ApiError 로 표면화돼 화면이 안내한다.
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
	// redirectOn401 은 과거 호환을 위해 받기만 하고 무시한다(인증 라우팅은 레이아웃 권위).
	opts: RequestInit & { redirectOn401?: boolean } = {}
): Promise<T> {
	const { redirectOn401: _ignored, ...init } = opts;
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
