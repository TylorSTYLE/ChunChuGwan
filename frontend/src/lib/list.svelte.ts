import { replaceState } from '$app/navigation';
import { api, ApiError } from '$lib/api';
import { filterUrl, queryString, type FilterVal } from '$lib/filters';

/** 클라이언트 사이드 페이지네이션·필터 목록 상태.
 *
 * load 가 준 초기 페이로드로 seed 하고, 페이지/필터가 바뀌면 전체 네비게이션 대신
 * api() 만 다시 호출해 목록 페이로드를 교체한다(스크롤·나머지 화면 유지). URL 은
 * replaceState 로만 동기화 — load 재실행·스크롤 점프가 없고 새로고침·딥링크는 유지된다.
 *
 * action.svelte.ts 의 createAction 과 같은 룬 모듈 패턴(.svelte.ts). 컴포넌트 초기화 시
 * 호출해야 내부 $effect(외부 data 변경 시 리시드)가 등록된다.
 *
 * 사용:
 *   const list = createList({
 *     source: () => data.logs, api: '/logs', route: '/log/archive',
 *     params: (d) => ({ domain: d.domain, status: d.status, limit: d.limit, page: d.page_num }),
 *     defaults: { limit: 25, page: 1 }, onError: (m) => (act.error = m)
 *   });
 *   const d = $derived(list.data);
 *   // 필터: list.go({ status: v, page: 1 })   페이지: list.go({ page: n })
 */
export function createList<T>(opts: {
	/** load 결과의 목록 페이로드 getter — 외부 변경(최초 진입·invalidateAll) 시 리시드 */
	source: () => T;
	/** api() 경로 베이스 (base·쿼리 제외), 예: '/logs'. 라우트 파라미터에 의존하면
	 *  함수로 줘서 go() 시점의 현재 값으로 평가한다(예: () => `/sites/${s.site.id}/lists`) */
	api: string | (() => string);
	/** filterUrl 라우트 베이스 (base·쿼리 제외), 예: '/log/archive'. api 와 같은 이유로 함수 허용 */
	route: string | (() => string);
	/** 초기 페이로드에서 초기 필터·페이지 파라미터를 뽑는다 (반응형 props 를 최상위에서
	 *  직접 읽지 않도록 클로저로 받아 초기 스냅샷에서만 추출) */
	params: (data: T) => Record<string, FilterVal>;
	/** 쿼리에서 생략할 기본값 (filterUrl/queryString 과 동일 규칙) */
	defaults?: Record<string, FilterVal>;
	/** 페치 실패 메시지 콜백 */
	onError?: (msg: string) => void;
}) {
	let view = $state(opts.source());
	let params = $state(opts.params(view));
	let busy = $state(false);
	// go() 요청 시퀀스 — 늦게 도착한 이전 응답이 최신 view/URL 을 덮어쓰지 않게 한다.
	let seq = 0;

	// load 결과가 바뀌면(최초 진입·액션 후 invalidateAll) 목록과 파라미터를 다시 seed 한다.
	// go() 는 view 만 직접 교체하고 source()=data 는 건드리지 않으므로 이 효과가 덮어쓰지 않는다.
	// params 도 함께 리시드해야 무필터로 재진입한 뒤 페이저를 눌러도 옛 필터가 되살아나지 않는다.
	$effect(() => {
		const next = opts.source();
		view = next;
		params = opts.params(next);
	});

	/** 파라미터를 patch 로 갱신하고 목록만 다시 받아 교체 + URL 동기화. */
	async function go(patch: Record<string, FilterVal>): Promise<void> {
		params = { ...params, ...patch };
		const qs = queryString(params, opts.defaults);
		const apiBase = typeof opts.api === 'function' ? opts.api() : opts.api;
		const routeBase = typeof opts.route === 'function' ? opts.route() : opts.route;
		const mySeq = ++seq;
		busy = true;
		try {
			const result = await api<T>(`${apiBase}${qs}`);
			if (mySeq !== seq) return; // 더 최신 go() 가 시작됨 — 이 응답은 버린다(stale)
			view = result;
			replaceState(filterUrl(routeBase, params, opts.defaults), {});
		} catch (err) {
			if (mySeq !== seq) return; // stale 오류도 무시(최신 요청이 화면을 관리)
			opts.onError?.(err instanceof ApiError ? err.message : String(err));
		} finally {
			if (mySeq === seq) busy = false; // 최신 요청만 busy 해제(이전 요청은 관여 안 함)
		}
	}

	return {
		get data() {
			return view;
		},
		get busy() {
			return busy;
		},
		go
	};
}
