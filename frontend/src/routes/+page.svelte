<script lang="ts">
	import { pagePath, snapPath } from '$lib/urls';
	import { base } from '$app/paths';
	import { t } from '$lib/i18n';
	import { filesize, ts } from '$lib/format';
	import type { Dashboard, RecentSnap, RecentLog } from '$lib/types';

	let { data }: { data: { dashboard: Dashboard } } = $props();
	const d = $derived(data.dashboard);

	const STATUS: Record<string, { cls: string; label: string }> = {
		new: { cls: 'new', label: '신규' },
		changed: { cls: 'changed', label: '변경' },
		unchanged: { cls: 'same', label: '동일' },
		forced_same: { cls: 'same', label: '동일(강제)' },
		error: { cls: 'error', label: '실패' }
	};

	function snapBadge(s: RecentSnap): { cls: string; label: string } {
		if (s.is_first) return { cls: 'new', label: '신규' };
		if (s.changed) return { cls: 'changed', label: '변경' };
		return { cls: 'same', label: '동일' };
	}
	function logBadge(log: RecentLog): { cls: string; label: string } {
		return STATUS[log.status] ?? { cls: 'same', label: log.status };
	}
</script>

<h2>{t('현황')}</h2>

<div class="stat-grid">
	<div class="stat-card">
		<div class="label">{t('사이트')}</div>
		<div class="value">{d.total_sites}</div>
	</div>
	<div class="stat-card">
		<div class="label">{t('아카이브 페이지')}</div>
		<div class="value">{d.total_pages}</div>
	</div>
	<div class="stat-card">
		<div class="label">{t('전체 스냅샷')}</div>
		<div class="value">{d.total_snapshots}</div>
	</div>
	<div class="stat-card">
		<div class="label">{t('이번 주 스냅샷')}</div>
		<div class="value">{d.week_count}</div>
	</div>
	<div class="stat-card">
		<div class="label">{t('최근 24시간')}</div>
		<div class="value">{d.recent_count}</div>
	</div>
	<div class="stat-card">
		<div class="label">{t('총 용량')}</div>
		<div class="value">{filesize(d.total_bytes)}</div>
	</div>
</div>

<h3>{t('용량 트렌드')}</h3>
<table class="trend-table">
	<thead>
		<tr>
			<th>{t('기간')}</th>
			<th>{t('스냅샷')}</th>
			<th>{t('용량')}</th>
			<th style="width:45%"></th>
		</tr>
	</thead>
	<tbody>
		{#each d.trend as row}
			<tr>
				<td>{t(row.label)}</td>
				<td class="num">{row.count}</td>
				<td class="num mono">{filesize(row.bytes)}</td>
				<td><div class="trend-bar" style="width: {row.pct.toFixed(1)}%"></div></td>
			</tr>
		{/each}
	</tbody>
</table>

<h3>{t('최근 아카이브')}</h3>
{#if d.recent_snaps.length === 0}
	<p class="muted">{t('아직 스냅샷이 없습니다.')}</p>
{:else}
	<div class="table-wrap">
		<table>
			<thead>
				<tr><th>{t('시간')}</th><th>{t('상태')}</th><th>URL</th><th>{t('용량')}</th><th></th></tr>
			</thead>
			<tbody>
				{#each d.recent_snaps as s}
					{@const b = snapBadge(s)}
					<tr>
						<td class="mono">{ts(s.taken_at)}</td>
						<td><span class="badge {b.cls}">{t(b.label)}</span></td>
						<td class="url-cell"
							><a href={pagePath(s.site_id, s.page_id)} title={s.page_url}>{s.page_url}</a></td
						>
						<td class="num mono">{filesize(s.bytes)}</td>
						<td><a href={snapPath(s.site_id, s.page_id, s.id)}>{t('보기')}</a></td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
{/if}

<h3>{t('최근 로그')}</h3>
{#if d.recent_logs.length === 0}
	<p class="muted">{t('로그가 없습니다. 아카이빙을 실행하면 결과가 여기에 기록됩니다.')}</p>
{:else}
	<div class="table-wrap wide">
		<table>
			<thead>
				<tr
					><th>{t('시간')}</th><th>{t('상태')}</th><th>URL</th><th>{t('소요')}</th><th
						>{t('출처')}</th
					><th></th></tr
				>
			</thead>
			<tbody>
				{#each d.recent_logs as log}
					{@const b = logBadge(log)}
					<tr>
						<td class="mono">{ts(log.started_at)}</td>
						<td><span class="badge {b.cls}">{t(b.label)}</span></td>
						<td class="url-cell">
							{#if log.page_id}
								<a href={pagePath(log.page_site_id, log.page_id)} title={log.url}>{log.url}</a>
							{:else}
								<span title={log.url}>{log.url}</span>
							{/if}
						</td>
						<td class="num mono">{log.duration_ms}ms</td>
						<td class="mono muted">{log.source}</td>
						<td>{#if log.snapshot_id}<a href={snapPath(log.page_site_id, log.page_id, log.snapshot_id)}>{t('보기')}</a>{/if}</td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
	<p class="more-link"><a href="{base}/log/archive">{t('전체 로그 →')}</a></p>
{/if}

<style>
	.trend-table {
		max-width: 680px;
	}
	.trend-bar {
		height: 12px;
		border-radius: 3px;
		background: var(--link);
		opacity: 0.35;
		min-width: 0;
	}
	td.url-cell {
		max-width: 420px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.more-link {
		font-size: 12px;
		margin-top: 8px;
	}
</style>
