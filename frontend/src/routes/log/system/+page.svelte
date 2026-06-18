<script lang="ts">
	import { base } from '$app/paths';
	import { goto } from '$app/navigation';
	import { t } from '$lib/i18n';
	import { ts } from '$lib/format';
	import type { SystemLogsData } from '$lib/types';

	let { data }: { data: { logs: SystemLogsData } } = $props();
	const d = $derived(data.logs);

	const LEVEL_BADGE: Record<string, string> = {
		DEBUG: 'same',
		INFO: 'same',
		WARNING: 'changed',
		ERROR: 'error',
		CRITICAL: 'error'
	};

	function applyFilter(patch: Record<string, string>) {
		const cur: Record<string, string> = {
			level: d.level,
			source: d.source,
			limit: String(d.limit)
		};
		Object.assign(cur, patch);
		const qs = new URLSearchParams();
		if (cur.level) qs.set('level', cur.level);
		if (cur.source) qs.set('source', cur.source);
		if (cur.limit && cur.limit !== '50') qs.set('limit', cur.limit);
		goto(`${base}/log/system${qs.toString() ? `?${qs}` : ''}`);
	}

	function pageUrl(n: number): string {
		const qs = new URLSearchParams();
		if (d.level) qs.set('level', d.level);
		if (d.source) qs.set('source', d.source);
		if (d.limit !== 50) qs.set('limit', String(d.limit));
		if (n > 1) qs.set('page', String(n));
		return `${base}/log/system${qs.toString() ? `?${qs}` : ''}`;
	}
</script>

<h2>{t('시스템 로그')}</h2>

<div class="toolbar">
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
</div>

{#if d.logs.length === 0}
	<p class="muted">{t('로그가 없습니다.')}</p>
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
	{#if d.total_pages > 1}
		<div class="pager">
			{#if d.page_num > 1}<a href={pageUrl(d.page_num - 1)}>← {t('이전')}</a>{/if}
			<span class="muted">{d.page_num} / {d.total_pages}</span>
			{#if d.page_num < d.total_pages}<a href={pageUrl(d.page_num + 1)}>{t('다음')} →</a>{/if}
		</div>
	{/if}
{/if}

<style>
	.toolbar .spacer {
		flex: 1;
	}
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
	.pager {
		display: flex;
		gap: 12px;
		margin-top: 10px;
	}
</style>
