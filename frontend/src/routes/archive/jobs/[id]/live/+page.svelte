<script lang="ts">
	import { onMount, untrack } from 'svelte';
	import { base } from '$app/paths';
	import { goto } from '$app/navigation';
	import { t } from '$lib/i18n';
	import { api } from '$lib/api';
	import type { LiveMeta, LiveState } from '$lib/types';

	let { data }: { data: { meta: LiveMeta } } = $props();
	// 한 페이지 = 한 라이브 세션(불변) — 클로저에서 쓰는 상수는 prop 에서 1회 캡처.
	const m = untrack(() => data.meta);
	const JOB = m.id;
	const VW = m.viewport_w;
	const VH = m.viewport_h;
	const OWNED = m.owned;

	let shotSrc = $state(`/api/web/live/${JOB}/shot?cb=${Date.now()}`);
	let guide = $state('');
	let done = $state(false);
	let textin = $state('');
	let forceBusy = $state(false);
	let img: HTMLImageElement | undefined = $state();
	let lastTime = 0;

	function refreshShot() {
		shotSrc = `/api/web/live/${JOB}/shot?cb=${Date.now()}`;
	}
	function delay(): number {
		const now = Date.now();
		const d = lastTime ? now - lastTime : 0;
		lastTime = now;
		return Math.min(d, 3000);
	}
	async function sendClick(kind: string, x: number, y: number) {
		try {
			await api(`/live/${JOB}/click`, {
				method: 'POST',
				body: JSON.stringify({ kind, x, y, delay_ms: delay() })
			});
		} catch {
			/* 일시 오류 — 다음 입력에서 회복 */
		}
	}
	async function sendKey(kind: string, key: string) {
		try {
			await api(`/live/${JOB}/key`, {
				method: 'POST',
				body: JSON.stringify({ kind, key, delay_ms: delay() })
			});
		} catch {
			/* 일시 오류 — 다음 입력에서 회복 */
		}
	}
	function scale(ev: MouseEvent): { x: number; y: number } {
		const r = (img as HTMLImageElement).getBoundingClientRect();
		const x = Math.round((ev.clientX - r.left) * (VW / (r.width || VW)));
		const y = Math.round((ev.clientY - r.top) * (VH / (r.height || VH)));
		return {
			x: Math.max(0, Math.min(x, VW - 1)),
			y: Math.max(0, Math.min(y, VH - 1))
		};
	}

	// ── 드래그/클릭 입력 (소유자만) ──
	let pressing = false;
	let lastMove = 0;
	let dragged = false;

	function onMouseDown(ev: MouseEvent) {
		if (!OWNED) return;
		ev.preventDefault();
		const p = scale(ev);
		pressing = true;
		dragged = false;
		lastMove = Date.now();
		sendClick('down', p.x, p.y);
		guide = `${t('누름')} ${p.x},${p.y}`;
	}
	function onMouseMove(ev: MouseEvent) {
		if (!OWNED || !pressing) return;
		const now = Date.now();
		if (now - lastMove < 60) return;
		lastMove = now;
		dragged = true;
		const p = scale(ev);
		sendClick('move', p.x, p.y);
	}
	function onMouseUp(ev: MouseEvent) {
		if (!OWNED || !pressing) return;
		pressing = false;
		const p = scale(ev);
		sendClick('up', p.x, p.y);
		guide = `${dragged ? t('드래그') : t('클릭')} ${p.x},${p.y}`;
	}

	function sendText() {
		if (!textin) return;
		sendKey('text', textin);
		guide = `${t('입력')}: ${textin}`;
		textin = '';
	}
	function sendEnter() {
		sendKey('key', 'Enter');
		guide = `${t('입력')}: Enter`;
	}
	async function forceSolve() {
		if (!confirm(t('로봇 확인을 직접 통과시켰다면 현재 화면으로 캡처를 진행합니다. 계속할까요?'))) return;
		forceBusy = true;
		guide = t('진행 요청됨 — 잠시만 기다리세요…');
		try {
			await api(`/live/${JOB}/solve`, { method: 'POST' });
		} catch {
			forceBusy = false; // 상태 폴링이 완료를 감지
		}
	}
	async function cancel() {
		if (!confirm(t('이 작업을 취소할까요?'))) return;
		try {
			await api(`/live/${JOB}/cancel`, { method: 'POST' });
		} catch {
			/* 상태 폴링이 종료를 감지 */
		}
		await goto(`${base}/archive/needs-human`);
	}

	onMount(() => {
		window.addEventListener('mouseup', onMouseUp);
		const shotTimer = setInterval(refreshShot, m.shot_interval_ms);
		const stateTimer = setInterval(async () => {
			try {
				const s = await api<LiveState>(`/live/${JOB}/state`);
				if (s.status === 'done') {
					clearInterval(shotTimer);
					clearInterval(stateTimer);
					done = true;
					guide = t('처리됨');
				}
			} catch {
				/* 폴링 실패는 다음 회차에서 회복 */
			}
		}, 1500);
		return () => {
			window.removeEventListener('mouseup', onMouseUp);
			clearInterval(shotTimer);
			clearInterval(stateTimer);
		};
	});
</script>

<div class="toolbar">
	<h2>{t('사람 확인 처리')}</h2>
	<a href="{base}/archive/needs-human">{t('목록')}</a>
</div>
<p class="muted url">{m.url}</p>

{#if done}
	<div class="done">
		{t('처리되었습니다 — 캡처를 이어서 진행합니다. 잠시 후 결과는 로그에서 확인하세요.')}
		<a href="{base}/archive/needs-human">{t('목록으로')}</a>
	</div>
{/if}

{#if !OWNED}
	<p class="notice">{t('다른 관리자가 처리 중입니다 — 보기 전용입니다.')}</p>
{/if}

<div class="live-bar">
	<button type="button" onclick={refreshShot}>{t('화면 갱신')}</button>
	{#if OWNED}
		<input type="text" bind:value={textin} placeholder={t('입력할 문자열…')} autocomplete="off" />
		<button type="button" onclick={sendText}>{t('문자 입력')}</button>
		<button type="button" onclick={sendEnter}>Enter</button>
		<button type="button" class="primary" onclick={forceSolve} disabled={forceBusy}
			>{t('사람 확인 완료')}</button
		>
		<button type="button" class="danger" onclick={cancel}>{t('취소')}</button>
	{/if}
	<span class="guide mono">{guide}</span>
</div>

<div class="live-frame">
	<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
	<img
		bind:this={img}
		id="shot"
		src={shotSrc}
		alt="live"
		width={VW}
		height={VH}
		onmousedown={onMouseDown}
		onmousemove={onMouseMove}
		oncontextmenu={(e) => e.preventDefault()}
		onerror={(e) => (e.currentTarget as HTMLImageElement).removeAttribute('src')}
	/>
</div>

<p class="muted">
	{t('화면 위를 클릭하면 서버 브라우저의 같은 위치를 누릅니다. 드래그도 그대로 전달됩니다. 챌린지(체크박스·그림 찾기 등)를 통과시키면 자동으로 캡처가 이어집니다.')}
</p>
{#if OWNED}
	<p class="muted">
		{t("로봇 확인을 통과했는데도 자동으로 진행되지 않으면 '사람 확인 완료'를 눌러 현재 화면 그대로 캡처를 진행시킬 수 있습니다.")}
	</p>
{/if}

<style>
	.toolbar {
		display: flex;
		gap: 12px;
		align-items: baseline;
		margin-bottom: 4px;
	}
	.toolbar h2 {
		margin: 0;
	}
	.url {
		word-break: break-all;
		margin: 0 0 10px;
	}
	.done {
		display: block;
		padding: 10px 14px;
		background: var(--bg-soft);
		border: 1px solid var(--border);
		border-radius: 4px;
		margin-bottom: 10px;
	}
	.notice {
		background: var(--green-bg);
		color: var(--green);
		border-radius: 4px;
		padding: 6px 12px;
		margin-bottom: 10px;
		font-size: 13px;
	}
	.live-bar {
		display: flex;
		gap: 8px;
		align-items: center;
		flex-wrap: wrap;
		margin: 10px 0;
	}
	.live-bar input {
		max-width: 260px;
	}
	.guide {
		color: var(--seal);
		min-height: 1.2em;
	}
	button.primary {
		color: #fff;
		background: var(--seal);
		border-color: var(--seal);
	}
	button.danger {
		color: #fff;
		background: var(--red);
		border-color: var(--red);
	}
	.live-frame {
		display: inline-block;
		border: 1px solid var(--border);
		background: #111;
		max-width: 100%;
	}
	#shot {
		display: block;
		max-width: 100%;
		height: auto;
		cursor: crosshair;
		user-select: none;
	}
</style>
