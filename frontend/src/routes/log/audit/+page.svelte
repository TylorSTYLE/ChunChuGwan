<script lang="ts">
	import { base } from '$app/paths';
	import { goto } from '$app/navigation';
	import { t } from '$lib/i18n';
	import { ts } from '$lib/format';
	import type { AuditLogsData } from '$lib/types';

	let { data }: { data: { audit: AuditLogsData } } = $props();
	const d = $derived(data.audit);

	const ACTION_BADGE: Record<string, string> = {
		archive: 'new',
		view: 'site',
		download: 'changed',
		admin: 'same'
	};

	function applyFilter(patch: Record<string, string>) {
		const cur: Record<string, string> = {
			action: d.action,
			actor: d.actor,
			limit: String(d.limit)
		};
		Object.assign(cur, patch);
		const qs = new URLSearchParams();
		if (cur.action) qs.set('action', cur.action);
		if (cur.actor) qs.set('actor', cur.actor);
		if (cur.limit && cur.limit !== '50') qs.set('limit', cur.limit);
		goto(`${base}/log/audit${qs.toString() ? `?${qs}` : ''}`);
	}

	function pageUrl(n: number): string {
		const qs = new URLSearchParams();
		if (d.action) qs.set('action', d.action);
		if (d.actor) qs.set('actor', d.actor);
		if (d.limit !== 50) qs.set('limit', String(d.limit));
		if (n > 1) qs.set('page', String(n));
		return `${base}/log/audit${qs.toString() ? `?${qs}` : ''}`;
	}

	function actionLabel(a: string): string {
		return d.action_labels[a] ?? a;
	}
</script>

<h2>{t('감사 로그')}</h2>
<p class="muted lead">{t('누가 아카이빙·열람·문서 다운로드·관리 작업을 했는지 기록')}</p>

<div class="toolbar">
	<select value={d.action} onchange={(e) => applyFilter({ action: e.currentTarget.value })}>
		<option value="">{t('모든 종류')}</option>
		{#each d.actions as a}<option value={a}>{actionLabel(a)}</option>{/each}
	</select>
	<select value={d.actor} onchange={(e) => applyFilter({ actor: e.currentTarget.value })}>
		<option value="">{t('모든 요청자')}</option>
		{#each d.actors as a}<option value={a}>{a}</option>{/each}
	</select>
	<span class="spacer"></span>
	<span class="muted">{t('총')} {d.total}{t('건')}</span>
</div>

{#if d.logs.length === 0}
	<p class="muted">{t('감사 기록이 없습니다.')}</p>
{:else}
	<div class="table-wrap wide">
		<table>
			<thead>
				<tr>
					<th class="col-time">{t('시간')}</th>
					<th>{t('종류')}</th>
					<th>{t('요청자')}</th>
					<th>{t('대상')}</th>
					<th>{t('내용')}</th>
				</tr>
			</thead>
			<tbody>
				{#each d.logs as log}
					<tr>
						<td class="mono col-time">{ts(log.created_at)}</td>
						<td><span class="badge {ACTION_BADGE[log.action] ?? 'same'}">{actionLabel(log.action)}</span></td>
						<td class="mono">{log.actor}</td>
						<td class="mono muted target">{log.target ?? '-'}</td>
						<td>{log.message}</td>
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
	.lead {
		font-size: 13px;
		margin: -6px 0 12px;
	}
	.toolbar .spacer {
		flex: 1;
	}
	.col-time {
		white-space: nowrap;
		min-width: 160px;
	}
	.target {
		max-width: 280px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.pager {
		display: flex;
		gap: 12px;
		margin-top: 10px;
	}
</style>
