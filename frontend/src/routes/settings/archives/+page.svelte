<script lang="ts">
	import { goto } from '$app/navigation';
	import { base } from '$app/paths';
	import { t } from '$lib/i18n';
	import { ts } from '$lib/format';
	import type { MyArchivesData } from '$lib/types';

	let { data }: { data: { data: MyArchivesData } } = $props();
	const d = $derived(data.data);

	const STATUS_LABELS: Record<string, string> = {
		new: '새 스냅샷',
		changed: '변경됨',
		unchanged: '변경 없음',
		forced_same: '강제 저장',
		error: '오류'
	};

	function nav(params: Record<string, string | number>) {
		const qs = new URLSearchParams();
		if (d.status) qs.set('status', d.status);
		if (d.limit !== 25) qs.set('limit', String(d.limit));
		for (const [k, v] of Object.entries(params)) {
			if (v) qs.set(k, String(v));
			else qs.delete(k);
		}
		const q = qs.toString();
		goto(`${base}/settings/archives${q ? `?${q}` : ''}`);
	}

	function onStatus(e: Event) {
		const v = (e.target as HTMLSelectElement).value;
		nav({ status: v, page: '' });
	}

	function fmtDuration(ms: number): string {
		if (!ms) return '';
		return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`;
	}
</script>

<h2>{t('내 아카이브')}</h2>
<p class="muted hint">{t('내가 대시보드·확장에서 직접 요청한 단발 아카이빙 이력입니다.')}</p>

<div class="filters">
	<select value={d.status} onchange={onStatus}>
		<option value="">{t('전체 상태')}</option>
		{#each d.statuses as s}<option value={s}>{t(STATUS_LABELS[s] ?? s)}</option>{/each}
	</select>
	<span class="muted">{t('총')} {d.total}{t('건')}</span>
</div>

{#if d.items.length > 0}
	<div class="table-wrap">
		<table>
			<thead>
				<tr>
					<th>{t('시각')}</th>
					<th>{t('상태')}</th>
					<th>{t('URL')}</th>
					<th>{t('소요')}</th>
				</tr>
			</thead>
			<tbody>
				{#each d.items as { log }}
					<tr>
						<td class="mono">{ts(log.started_at)}</td>
						<td>{t(STATUS_LABELS[log.status] ?? log.status)}</td>
						<td class="url">
							{#if log.page_id}
								<a href="{base}/page/{log.page_id}">{log.url}</a>
							{:else}
								{log.url}
							{/if}
						</td>
						<td class="mono muted">{fmtDuration(log.duration_ms)}</td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>

	{#if d.total_pages > 1}
		<div class="pager">
			<button disabled={d.page_num <= 1} onclick={() => nav({ page: d.page_num - 1 })}
				>{t('이전')}</button
			>
			<span class="muted">{d.page_num} / {d.total_pages}</span>
			<button
				disabled={d.page_num >= d.total_pages}
				onclick={() => nav({ page: d.page_num + 1 })}>{t('다음')}</button
			>
		</div>
	{/if}
{:else}
	<p class="muted">{t('아직 요청한 아카이빙이 없습니다.')}</p>
{/if}

<style>
	.hint {
		font-size: 12px;
		margin: 0 0 12px;
	}
	.filters {
		display: flex;
		gap: 12px;
		align-items: center;
		margin-bottom: 12px;
		font-size: 13px;
	}
	td.url {
		word-break: break-all;
	}
	.pager {
		display: flex;
		gap: 12px;
		align-items: center;
		margin-top: 12px;
		font-size: 13px;
	}
</style>
