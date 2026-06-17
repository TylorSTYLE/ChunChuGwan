<script lang="ts">
	import { base } from '$app/paths';
	import { goto } from '$app/navigation';
	import { t } from '$lib/i18n';
	import { ts } from '$lib/format';
	import type { LogsData } from '$lib/types';

	let { data }: { data: { logs: LogsData } } = $props();
	const d = $derived(data.logs);

	const STATUS_LABEL: Record<string, string> = {
		new: '신규',
		changed: '변경',
		unchanged: '동일',
		forced_same: '동일(강제)',
		error: '실패'
	};
	const BADGE: Record<string, string> = {
		new: 'new',
		changed: 'changed',
		unchanged: 'same',
		forced_same: 'same',
		error: 'error'
	};

	function applyFilter(patch: Record<string, string>) {
		const qs = new URLSearchParams();
		const cur: Record<string, string> = {
			domain: d.domain,
			status: d.status,
			limit: String(d.limit)
		};
		Object.assign(cur, patch);
		if (cur.domain) qs.set('domain', cur.domain);
		if (cur.status) qs.set('status', cur.status);
		if (cur.limit && cur.limit !== '25') qs.set('limit', cur.limit);
		goto(`${base}/logs${qs.toString() ? `?${qs}` : ''}`);
	}

	function pageUrl(n: number): string {
		const qs = new URLSearchParams();
		if (d.domain) qs.set('domain', d.domain);
		if (d.status) qs.set('status', d.status);
		if (d.limit !== 25) qs.set('limit', String(d.limit));
		if (n > 1) qs.set('page', String(n));
		return `${base}/logs${qs.toString() ? `?${qs}` : ''}`;
	}
</script>

<h2>{t('아카이빙 로그')}</h2>

<div class="toolbar">
	<select value={d.domain} onchange={(e) => applyFilter({ domain: e.currentTarget.value })}>
		<option value="">{t('전체 도메인')}</option>
		{#each d.domains as dom}<option value={dom}>{dom}</option>{/each}
	</select>
	<select value={d.status} onchange={(e) => applyFilter({ status: e.currentTarget.value })}>
		<option value="">{t('전체 상태')}</option>
		{#each d.statuses as st}<option value={st}>{t(STATUS_LABEL[st] ?? st)}</option>{/each}
	</select>
	<span class="spacer"></span>
	<span class="muted">{t('총')} {d.total}{t('건')}</span>
</div>

{#if d.items.length === 0}
	<p class="muted">{t('로그가 없습니다.')}</p>
{:else}
	<div class="table-wrap wide">
		<table>
			<thead>
				<tr>
					<th>{t('시각')}</th>
					<th>{t('상태')}</th>
					<th>URL</th>
					<th>{t('소요')}</th>
					<th>{t('출처')}</th>
					<th></th>
				</tr>
			</thead>
			<tbody>
				{#each d.items as it}
					<tr>
						<td class="mono">{ts(it.log.started_at)}</td>
						<td>
							<span class="badge {BADGE[it.log.status] ?? 'same'}"
								>{t(STATUS_LABEL[it.log.status] ?? it.log.status)}</span
							>
						</td>
						<td class="url-cell">
							{#if it.log.page_id}
								<a href="{base}/page/{it.log.page_id}" title={it.log.url}>{it.log.url}</a>
							{:else}
								<span title={it.log.url}>{it.log.url}</span>
							{/if}
						</td>
						<td class="num mono">{it.log.duration_ms}ms</td>
						<td class="mono muted">{it.log.source}</td>
						<td
							>{#if it.log.snapshot_id}<a href="{base}/snapshot/{it.log.snapshot_id}">{t('보기')}</a
								>{/if}</td
						>
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
	td.url-cell {
		max-width: 420px;
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
