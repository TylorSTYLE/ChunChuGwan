<script lang="ts">
	import { t } from '$lib/i18n';
	import { ts } from '$lib/format';
	import { filterUrl } from '$lib/filters';
	import { createList } from '$lib/list.svelte';
	import type { SystemLogsData } from '$lib/types';
	import Toolbar from '$lib/components/Toolbar.svelte';
	import Pager from '$lib/components/Pager.svelte';
	import PageSize from '$lib/components/PageSize.svelte';
	import EmptyState from '$lib/components/EmptyState.svelte';
	import AlertBox from '$lib/components/AlertBox.svelte';
	import { Badge, type BadgeVariant } from '$lib/components/ui/badge';

	let { data }: { data: { logs: SystemLogsData } } = $props();

	let listError = $state('');
	const ROUTE = '/log/system';
	const FILTER_DEF = { limit: 25, page: 1 };
	const list = createList({
		source: () => data.logs,
		api: '/system/logs',
		route: ROUTE,
		params: (d) => ({ level: d.level, source: d.source, limit: d.limit, page: d.page_num }),
		defaults: FILTER_DEF,
		onError: (m) => (listError = m)
	});
	const d = $derived(list.data);

	const LEVEL_BADGE: Record<string, BadgeVariant> = {
		DEBUG: 'same',
		INFO: 'same',
		WARNING: 'changed',
		ERROR: 'error',
		CRITICAL: 'error'
	};

	const applyFilter = (patch: Record<string, string>) => list.go({ ...patch, page: 1 });
	const pageUrl = (n: number) =>
		filterUrl(ROUTE, { level: d.level, source: d.source, limit: d.limit, page: n }, FILTER_DEF);
</script>

<h2>{t('시스템 로그')}</h2>
<AlertBox error={listError} />

<Toolbar>
	<select value={d.level} onchange={(e) => applyFilter({ level: e.currentTarget.value })}>
		<option value="">{t('전체 레벨')}</option>
		{#each d.levels as lv}<option value={lv}>{lv}</option>{/each}
	</select>
	<select value={d.source} onchange={(e) => applyFilter({ source: e.currentTarget.value })}>
		<option value="">{t('전체 출처')}</option>
		{#each d.sources as sc}<option value={sc}>{sc}</option>{/each}
	</select>
	<span class="spacer"></span>
	<span class="muted">{t('총')} {d.total}{t('건')}</span>
	<PageSize value={d.limit} options={d.limits} onchange={(n) => list.go({ limit: n, page: 1 })} />
</Toolbar>

{#if d.logs.length === 0}
	<EmptyState message={t('로그가 없습니다.')} />
{:else}
	<div class="table-wrap wide cards">
		<table>
			<thead>
				<tr>
					<th>{t('시간')}</th>
					<th>{t('레벨')}</th>
					<th>{t('출처')}</th>
					<th>{t('메시지')}</th>
				</tr>
			</thead>
			<tbody>
				{#each d.logs as log}
					<tr>
						<td class="mono" data-label={t('시간')}>{ts(log.created_at)}</td>
						<td data-label={t('레벨')}><Badge variant={LEVEL_BADGE[log.level] ?? 'same'}>{log.level}</Badge></td>
						<td class="mono muted" data-label={t('출처')}>{log.source}</td>
						<td data-label={t('메시지')}>
							<span class="mono muted">{log.logger}</span>
							<div>{log.message}</div>
							{#if log.traceback}
								<details>
									<summary class="muted">traceback</summary>
									<pre class="tb">{log.traceback}</pre>
								</details>
							{/if}
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
	/* 시간 컬럼은 한 줄 유지 — 폭이 좁아 줄바꿈되던 문제 보정 */
	th:first-child,
	td.mono:first-child {
		white-space: nowrap;
		min-width: 160px;
	}
	.tb {
		font-size: 11px;
		white-space: pre-wrap;
		overflow-wrap: anywhere;
		background: var(--bg-soft);
		padding: 8px;
		border-radius: 4px;
	}
</style>
