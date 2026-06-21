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

/** 서버 오류 응답에서 사람이 읽을 메시지를 뽑는다.
 *
 * FastAPI 는 HTTPException 이면 `detail` 에 문자열을, 요청 검증 실패(422)면
 * `detail` 에 `[{loc, msg, type}, ...]` 배열을 담는다. 배열을 그대로
 * Error.message 로 넘기면 `[object Object],...` 로 직렬화돼 화면에 노출되므로
 * 여기서 항상 문자열로 정규화한다(필드명 + 메시지). */
export function errorDetail(body: unknown, fallback: string): string {
	if (!body || typeof body !== 'object') return fallback;
	const detail = (body as { detail?: unknown }).detail;
	if (typeof detail === 'string' && detail) return detail;
	if (Array.isArray(detail)) {
		const msgs = detail
			.map((item) => {
				if (!item || typeof item !== 'object' || !('msg' in item))
					return typeof item === 'string' ? item : '';
				const rawLoc = (item as { loc?: unknown }).loc;
				const loc = Array.isArray(rawLoc)
					? rawLoc.filter((p) => p !== 'body').join('.')
					: '';
				const msg = String((item as { msg?: unknown }).msg ?? '');
				return loc ? `${loc}: ${msg}` : msg;
			})
			.filter(Boolean);
		if (msgs.length) return msgs.join('; ');
	}
	return fallback;
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
			detail = errorDetail(await res.json(), res.statusText);
		} catch {
			/* JSON 아님 — statusText 유지 */
		}
		throw new ApiError(res.status, detail);
	}
	return res.json() as Promise<T>;
}

/** POST 로 파일(tar.gz)을 받아 브라우저 다운로드를 트리거한다 — 백업·내보내기용.
 *
 * api() 는 응답을 JSON 으로 파싱하므로 바이너리 다운로드엔 쓸 수 없다. 같은
 * 세션 쿠키·CSRF 헤더로 호출하되 Blob 으로 받아 a[download] 로 저장한다.
 */
export async function download(path: string): Promise<void> {
	const res = await fetch(`/api/web${path}`, {
		method: 'POST',
		credentials: 'same-origin',
		headers: { 'X-Requested-With': 'fetch' }
	});
	if (!res.ok) {
		let detail = res.statusText;
		try {
			detail = errorDetail(await res.json(), res.statusText);
		} catch {
			/* JSON 아님 */
		}
		throw new ApiError(res.status, detail);
	}
	const blob = await res.blob();
	const cd = res.headers.get('content-disposition') ?? '';
	const m = cd.match(/filename\*?=(?:UTF-8'')?"?([^";]+)"?/i);
	const name = m ? decodeURIComponent(m[1]) : 'download';
	const url = URL.createObjectURL(blob);
	const a = document.createElement('a');
	a.href = url;
	a.download = name;
	a.click();
	URL.revokeObjectURL(url);
}
