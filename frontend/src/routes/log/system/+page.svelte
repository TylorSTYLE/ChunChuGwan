<script lang="ts">
	import { goto } from '$app/navigation';
	import { t } from '$lib/i18n';
	import { ts } from '$lib/format';
	import { filterUrl } from '$lib/filters';
	import type { SystemLogsData } from '$lib/types';
	import Toolbar from '$lib/components/Toolbar.svelte';
	import Pager from '$lib/components/Pager.svelte';
	import EmptyState from '$lib/components/EmptyState.svelte';

	let { data }: { data: { logs: SystemLogsData } } = $props();
	const d = $derived(data.logs);

	const LEVEL_BADGE: Record<string, string> = {
		DEBUG: 'same',
		INFO: 'same',
		WARNING: 'changed',
		ERROR: 'error',
		CRITICAL: 'error'
	};

	const FILTER_DEF = { limit: 50, page: 1 };
	function applyFilter(patch: Record<string, string>) {
		goto(filterUrl('/log/system', { level: d.level, source: d.source, limit: d.limit, ...patch }, FILTER_DEF));
	}
	const pageUrl = (n: number) =>
		filterUrl('/log/system', { level: d.level, source: d.source, limit: d.limit, page: n }, FILTER_DEF);
</script>

<h2>{t('시스템 로그')}</h2>

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
</Toolbar>

{#if d.logs.length === 0}
	<EmptyState message={t('로그가 없습니다.')} />
{:else}
	<div class="table-wrap wide">
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
						<td class="mono">{ts(log.created_at)}</td>
						<td><span class="badge {LEVEL_BADGE[log.level] ?? 'same'}">{log.level}</span></td>
						<td class="mono muted">{log.source}</td>
						<td>
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
	<Pager page={d.page_num} totalPages={d.total_pages} href={pageUrl} />
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
