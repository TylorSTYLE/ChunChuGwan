import { invalidateAll } from '$app/navigation';
import { ApiError } from '$lib/api';

/** 비동기 작업 상태(busy·error·notice) + 실행 헬퍼.
 *
 * 페이지마다 반복되던 run() 패턴(busy 토글 → 성공 시 notice + invalidateAll →
 * 실패 시 error 메시지)을 공통화한다. Svelte 5 runes 라 `.svelte.ts` 모듈이다.
 *
 * 사용:
 *   const act = createAction();
 *   <AlertBox error={act.error} notice={act.notice} />
 *   <button disabled={act.busy} onclick={() => act.run(() => api(...), t('저장했습니다.'))}>
 */
export function createAction() {
	let busy = $state(false);
	let error = $state('');
	let notice = $state('');

	async function run(fn: () => Promise<unknown>, ok = ''): Promise<void> {
		busy = true;
		error = '';
		notice = '';
		try {
			await fn();
			if (ok) notice = ok;
			await invalidateAll();
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
		} finally {
			busy = false;
		}
	}

	return {
		get busy() {
			return busy;
		},
		set busy(v: boolean) {
			busy = v;
		},
		get error() {
			return error;
		},
		set error(v: string) {
			error = v;
		},
		get notice() {
			return notice;
		},
		set notice(v: string) {
			notice = v;
		},
		run
	};
}
