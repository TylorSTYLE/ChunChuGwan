<script lang="ts">
	import { pagePath, snapPath } from '$lib/urls';
	import { onMount } from 'svelte';
	import { base } from '$app/paths';
	import { goto, invalidateAll } from '$app/navigation';
	import { t } from '$lib/i18n';
	import { ts } from '$lib/format';
	import { api, ApiError } from '$lib/api';
	import type { CrawlDetail } from '$lib/types';
	import AlertBox from '$lib/components/AlertBox.svelte';
	import { createAction } from '$lib/action.svelte';
	import { Badge, type BadgeVariant } from '$lib/components/ui/badge';
	import { Button } from '$lib/components/ui/button';

	let { data }: { data: { detail: CrawlDetail; merged: boolean } } = $props();
	const d = $derived(data.detail);
	const c = $derived(d.crawl);
	const counts = $derived(d.counts);
	const action = createAction();

	const STATUS_BADGE: Record<string, BadgeVariant> = {
		running: 'running',
		done: 'new',
		cancelled: 'same'
	};
	const STATUS_LABEL: Record<string, string> = {
		running: '진행 중',
		done: '완료됨',
		cancelled: '취소됨'
	};
	const PAGE_BADGE: Record<string, BadgeVariant> = {
		done: 'new',
		failed: 'error',
		in_progress: 'running'
	};
	const PAGE_LABEL: Record<string, string> = {
		done: '완료',
		failed: '실패',
		in_progress: '아카이빙 중'
	};

	const FILTERS: [string, string][] = [
		['', '전체'],
		['pending', '대기'],
		['in_progress', '아카이빙 중'],
		['done', '완료'],
		['failed', '실패']
	];
	function filterCount(value: string): number {
		return value ? (counts[value as keyof typeof counts] as number) : counts.total;
	}

	const cancel = () => action.run(() => api(`/crawls/${c.id}/cancel`, { method: 'POST' }), t('크롤을 취소했습니다.'));
	const retryAll = () =>
		action.run(() => api(`/crawls/${c.id}/retry`, { method: 'POST' }), t('실패한 페이지를 다시 시도합니다.'));
	const retryPage = (pageId: number) =>
		action.run(
			() => api(`/crawls/${c.id}/pages/${pageId}/retry`, { method: 'POST' }),
			t('재시도가 등록되었습니다 — 크롤러가 곧 다시 시도합니다.')
		);

	async function rerun() {
		if (!confirm(`${c.start_url}\n\n${t('같은 범위·옵션으로 사이트 전체를 다시 아카이빙합니다. 계속할까요?')}`)) return;
		action.busy = true;
		action.error = '';
		try {
			const r = await api<{ crawl_id: number; merged: boolean }>(`/sites/${c.site_id}/crawls/${c.id}/rerun`, {
				method: 'POST'
			});
			await goto(`${base}/crawls/${r.crawl_id}${r.merged ? '?merged=1' : ''}`, { invalidateAll: true });
		} catch (err) {
			action.error = err instanceof ApiError ? err.message : String(err);
			action.busy = false;
		}
	}

	// 진행 중이면 5초마다 상태를 폴링해 변화가 있으면 다시 불러온다.
	onMount(() => {
		if (c.status !== 'running') return;
		let last = JSON.stringify(counts);
		const timer = setInterval(async () => {
			try {
				const s = await api<{ status: string; counts: typeof counts }>(`/crawls/${c.id}/status`);
				if (s.status !== 'running' || JSON.stringify(s.counts) !== last) {
					last = JSON.stringify(s.counts);
					await invalidateAll();
				}
			} catch {
				/* 일시 오류 — 다음 폴링에서 회복 */
			}
		}, 5000);
		return () => clearInterval(timer);
	});
</script>

<div class="toolbar">
	<h2 class="mono">{c.start_url}</h2>
	<span class="spacer"></span>
	{#if d.can_archive}
		{#if c.status === 'running'}
			<Button variant="outline" size="sm" onclick={cancel} disabled={action.busy}>{t('취소')}</Button>
		{/if}
		{#if counts.failed > 0}
			<Button variant="outline" size="sm" onclick={retryAll} disabled={action.busy}>{t('실패 일괄 재시도')}</Button>
		{/if}
		{#if c.status !== 'running'}
			<Button variant="outline" size="sm" onclick={rerun} disabled={action.busy}>{t('다시 아카이빙')}</Button>
		{/if}
	{/if}
	<a href="{base}/archive/list">{t('목록으로')}</a>
</div>

<AlertBox error={action.error} notice={action.notice} />
{#if data.merged}
	<div class="notice">
		{t('같은 사이트의 아카이브가 이미 진행 중이라 이 크롤에 병합되었습니다 (기존 옵션 유지).')}
	</div>
{/if}

<table class="meta">
	<tbody>
		<tr>
			<th>{t('상태')}</th>
			<td><Badge variant={STATUS_BADGE[c.status] ?? 'same'}>{t(STATUS_LABEL[c.status] ?? c.status)}</Badge></td>
		</tr>
		<tr><th>{t('범위')}</th><td class="mono">{c.scope_host}{c.scope_path}</td></tr>
		{#if d.network_tag}
			<tr><th>{t('로컬 네트워크 태그')}</th><td>{d.network_tag.name}</td></tr>
		{/if}
		<tr>
			<th>{t('옵션')}</th>
			<td class="mono">
				{t('최대 페이지 수')} {c.max_pages} · {t('최대 깊이')} {c.max_depth} · {t('페이지 간 간격(초)')} {c.delay_seconds}
			</td>
		</tr>
		<tr>
			<th>{t('실패 재시도')}</th>
			<td class="mono">
				{d.retry_backoff_labels.join(' → ')}
				<span class="muted">— {t('대기 후 재시도')} ({t('최대')} {d.max_attempts}{t('회')})</span>
			</td>
		</tr>
		<tr><th>{t('등록 시각')}</th><td class="mono">{ts(c.created_at)}</td></tr>
		{#if c.finished_at}
			<tr><th>{t('종료 시각')}</th><td class="mono">{ts(c.finished_at)}</td></tr>
		{/if}
	</tbody>
</table>

<div class="stat-grid counts">
	<div class="stat-card"><div class="label">{t('전체')}</div><div class="value">{counts.total}</div></div>
	<div class="stat-card"><div class="label">{t('완료')}</div><div class="value">{counts.done}</div></div>
	<div class="stat-card">
		<div class="label">{t('대기')}</div>
		<div class="value">{counts.pending + counts.in_progress}</div>
	</div>
	<div class="stat-card"><div class="label">{t('실패')}</div><div class="value">{counts.failed}</div></div>
</div>

<h3>{t('페이지')}</h3>
<p class="muted filters">
	{t('필터')}:
	{#each FILTERS as [value, label], i}
		{#if i > 0}·{/if}
		{#if d.status_filter === value}
			<strong>{t(label)} {filterCount(value)}</strong>
		{:else}
			<a href="{base}/crawls/{c.id}{value ? `?status=${value}` : ''}">{t(label)} {filterCount(value)}</a>
		{/if}
	{/each}
</p>

<div class="table-wrap">
	<table>
		<thead>
			<tr>
				<th>URL</th><th class="num">{t('깊이')}</th><th>{t('상태')}</th>
				<th class="num">{t('시도')}</th><th>{t('결과')}</th>
			</tr>
		</thead>
		<tbody>
			{#each d.pages as p}
				<tr>
					<td class="url-cell mono"><span title={p.url}>{p.url}</span></td>
					<td class="num mono">{p.depth}</td>
					<td>
						{#if PAGE_BADGE[p.status]}
							<Badge variant={PAGE_BADGE[p.status]}>{t(PAGE_LABEL[p.status])}</Badge>
						{:else if p.next_attempt_at}
							<Badge variant="changed">{t('재시도 대기')}</Badge>
						{:else}
							<Badge variant="same">{t('대기')}</Badge>
						{/if}
					</td>
					<td class="num mono">{p.attempts || '—'}</td>
					<td>
						{#if p.snapshot_id}
							<a href={snapPath(p.snapshot_site_id, p.snapshot_page_id, p.snapshot_id)}>{t('스냅샷')}</a>
							{#if p.snapshot_page_id}
								· <a href={pagePath(p.snapshot_site_id, p.snapshot_page_id)}>{t('타임라인')}</a>
							{/if}
						{:else if p.error}
							<span class="mono muted">{p.error}</span>
							{#if p.next_attempt_at}
								<span class="muted">— {t('재시도')} {ts(p.next_attempt_at)}</span>
							{/if}
							{#if d.can_archive && p.status === 'failed'}
								<button class="link-btn" onclick={() => retryPage(p.id)} disabled={action.busy}>{t('재시도')}</button>
							{/if}
						{:else}—{/if}
					</td>
				</tr>
			{/each}
		</tbody>
	</table>
</div>

<style>
	.toolbar h2 {
		margin: 0;
		word-break: break-all;
	}
	table.meta {
		max-width: 760px;
		margin-bottom: 16px;
	}
	table.meta th {
		text-align: left;
		white-space: nowrap;
		width: 1px;
		padding-right: 16px;
	}
	.stat-grid.counts {
		max-width: 760px;
	}
	.filters {
		margin: 0 0 8px;
	}
	.link-btn {
		border: none;
		background: none;
		color: var(--link);
		padding: 0 0 0 6px;
		font: inherit;
		font-size: 13px;
		cursor: pointer;
	}
	.link-btn:hover {
		text-decoration: underline;
		background: none;
	}
</style>
