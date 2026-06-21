<script lang="ts">
	import { base } from '$app/paths';
	import { t } from '$lib/i18n';
	import { filesize, ts } from '$lib/format';
	import { api } from '$lib/api';
	import { filterUrl } from '$lib/filters';
	import { createList } from '$lib/list.svelte';
	import type { TrashData, TrashEntry } from '$lib/types';
	import AlertBox from '$lib/components/AlertBox.svelte';
	import Toolbar from '$lib/components/Toolbar.svelte';
	import Pager from '$lib/components/Pager.svelte';
	import PageSize from '$lib/components/PageSize.svelte';
	import EmptyState from '$lib/components/EmptyState.svelte';
	import { createAction } from '$lib/action.svelte';
	import { Button } from '$lib/components/ui/button';

	let { data }: { data: { trash: TrashData } } = $props();
	const act = createAction();

	// 복원/영구삭제 액션 후 invalidateAll → 현재 페이지로 reseed.
	const ROUTE = '/archive/trash';
	const FILTER_DEF = { limit: 25, page: 1 };
	const list = createList({
		source: () => data.trash,
		api: '/trash',
		route: ROUTE,
		params: (d) => ({ limit: d.limit, page: d.page_num }),
		defaults: FILTER_DEF,
		onError: (m) => (act.error = m)
	});
	const d = $derived(list.data);
	const pageUrl = (n: number) => filterUrl(ROUTE, { limit: d.limit, page: n }, FILTER_DEF);

	function restore(e: TrashEntry) {
		return act.run(
			() => api(`/trash/${e.id}/restore`, { method: 'POST' }),
			t('복원했습니다.')
		);
	}

	function purge(e: TrashEntry) {
		if (!confirm(t('이 항목을 영구 삭제할까요? 되돌릴 수 없습니다.'))) return;
		return act.run(
			() => api(`/trash/${e.id}/purge`, { method: 'POST' }),
			t('영구 삭제했습니다.')
		);
	}
</script>

<h2>{t('휴지통')}</h2>
<p class="desc muted">
	{t('삭제한 아카이브가 여기에 보관됩니다 — 복원하거나 영구 삭제할 수 있습니다.')}
</p>

{#if !d.trash_enabled}
	<div class="notice off">
		{t('휴지통 기능이 꺼져 있어 삭제 시 즉시 영구 삭제됩니다. 아래는 이전에 보관된 항목입니다.')}
	</div>
{/if}
<p class="muted retention">
	{d.retention_days > 0
		? `${t('보관 기간')}: ${d.retention_days}${t('일')}`
		: t('자동 영구삭제가 꺼져 있습니다 (수동 삭제 전까지 보관).')}
</p>

<AlertBox error={act.error} notice={act.notice} />

{#if d.entries.length === 0}
	<EmptyState message={t('휴지통이 비어 있습니다.')} />
{:else}
	<Toolbar>
		<span class="spacer"></span>
		<span class="muted">{t('총')} {d.total}{t('건')}</span>
		<PageSize value={d.limit} onchange={(n) => list.go({ limit: n, page: 1 })} />
	</Toolbar>
	<div class="table-wrap wide cards">
		<table>
			<thead>
				<tr>
					<th>{t('종류')}</th>
					<th>{t('대상')}</th>
					<th>{t('스냅샷')}</th>
					<th>{t('용량')}</th>
					<th>{t('삭제 시각')}</th>
					<th>{t('삭제자')}</th>
					<th>{t('보관 기한')}</th>
					<th></th>
				</tr>
			</thead>
			<tbody>
				{#each d.entries as e}
					<tr>
						<td data-label={t('종류')}>
							<span class="badge {e.kind}">{e.kind === 'site' ? t('사이트') : t('페이지')}</span>
						</td>
						<td class="url-cell" data-label={t('대상')} title={e.label}>{e.label}</td>
						<td class="num" data-label={t('스냅샷')}>
							{e.snapshot_count}{#if e.kind === 'site'}<span class="muted"> ({e.page_count} {t('페이지')})</span>{/if}
						</td>
						<td class="num mono" data-label={t('용량')}>{filesize(e.bytes)}</td>
						<td class="mono" data-label={t('삭제 시각')}>{ts(e.deleted_at)}</td>
						<td class="muted" data-label={t('삭제자')}>{e.deleted_by_name || e.deleted_by_email || t('시스템')}</td>
						<td class="mono" data-label={t('보관 기한')}>{e.expires_at ? ts(e.expires_at) : '-'}</td>
						<td class="actions">
							<Button variant="outline" size="sm" onclick={() => restore(e)} disabled={act.busy}>{t('복원')}</Button>
							<Button variant="destructive" size="sm" onclick={() => purge(e)} disabled={act.busy}>{t('영구 삭제')}</Button>
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
	<Pager
		page={d.page_num}
		totalPages={d.total_pages}
		href={pageUrl}
		onpage={(n) => list.go({ page: n })}
		busy={list.busy}
	/>
{/if}

<style>
	.desc {
		margin: 0 0 4px;
		font-size: 13px;
	}
	.retention {
		font-size: 12px;
		margin: 0 0 12px;
	}
	.notice.off {
		margin: 8px 0;
		padding: 8px 12px;
		border: 1px solid var(--border);
		border-radius: 4px;
		background: var(--bg-soft);
		font-size: 13px;
	}
	td.url-cell {
		max-width: 360px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	td.actions {
		white-space: nowrap;
		display: flex;
		gap: 6px;
	}
	.badge.site {
		background: var(--amber-bg);
		color: var(--amber);
	}
</style>
