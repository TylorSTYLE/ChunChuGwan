<script lang="ts">
	import { pagePath, snapPath } from '$lib/urls';
	import { base } from '$app/paths';
	import { t } from '$lib/i18n';
	import { filesize, ts } from '$lib/format';
	import type { Dashboard, RecentSnap, RecentLog } from '$lib/types';
	import StatGrid from '$lib/components/StatGrid.svelte';
	import StatCard from '$lib/components/StatCard.svelte';
	import EmptyState from '$lib/components/EmptyState.svelte';
	import { Badge, type BadgeVariant } from '$lib/components/ui/badge';

	let { data }: { data: { dashboard: Dashboard } } = $props();
	const d = $derived(data.dashboard);

	const STATUS: Record<string, { cls: BadgeVariant; label: string }> = {
		new: { cls: 'new', label: '신규' },
		changed: { cls: 'changed', label: '변경' },
		unchanged: { cls: 'same', label: '동일' },
		forced_same: { cls: 'same', label: '동일(강제)' },
		error: { cls: 'error', label: '실패' }
	};

	function snapBadge(s: RecentSnap): { cls: BadgeVariant; label: string } {
		if (s.is_first) return { cls: 'new', label: '신규' };
		if (s.changed) return { cls: 'changed', label: '변경' };
		return { cls: 'same', label: '동일' };
	}
	function logBadge(log: RecentLog): { cls: BadgeVariant; label: string } {
		return STATUS[log.status] ?? { cls: 'same', label: log.status };
	}
</script>

<h2>{t('현황')}</h2>

<StatGrid>
	<StatCard label={t('사이트')} value={d.total_sites} />
	<StatCard label={t('아카이브 페이지')} value={d.total_pages} />
	<StatCard label={t('전체 스냅샷')} value={d.total_snapshots} />
	<StatCard label={t('이번 주 스냅샷')} value={d.week_count} />
	<StatCard label={t('최근 24시간')} value={d.recent_count} />
	<StatCard label={t('총 용량')} value={filesize(d.total_bytes)} />
</StatGrid>

<h3>{t('용량 트렌드')}</h3>
<div class="table-wrap">
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
</div>

<h3>{t('최근 아카이브')}</h3>
{#if d.recent_snaps.length === 0}
	<EmptyState message={t('아직 스냅샷이 없습니다.')} />
{:else}
	<div class="table-wrap cards">
		<table>
			<thead>
				<tr><th>{t('시간')}</th><th>{t('상태')}</th><th>URL</th><th>{t('용량')}</th><th></th></tr>
			</thead>
			<tbody>
				{#each d.recent_snaps as s}
					{@const b = snapBadge(s)}
					<tr>
						<td class="mono" data-label={t('시간')}>{ts(s.taken_at)}</td>
						<td data-label={t('상태')}><Badge variant={b.cls}>{t(b.label)}</Badge></td>
						<td class="url-cell" data-label="URL"><a href={pagePath(s.site_id, s.page_id)} title={s.page_url}>{s.page_url}</a></td>
						<td class="num mono" data-label={t('용량')}>{filesize(s.bytes)}</td>
						<td><a href={snapPath(s.site_id, s.page_id, s.id)}>{t('보기')}</a></td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
{/if}

<h3>{t('최근 로그')}</h3>
{#if d.recent_logs.length === 0}
	<EmptyState message={t('로그가 없습니다. 아카이빙을 실행하면 결과가 여기에 기록됩니다.')} />
{:else}
	<div class="table-wrap wide cards">
		<table>
			<thead>
				<tr><th>{t('시간')}</th><th>{t('상태')}</th><th>URL</th><th>{t('소요')}</th><th>{t('출처')}</th><th></th></tr>
			</thead>
			<tbody>
				{#each d.recent_logs as log}
					{@const b = logBadge(log)}
					<tr>
						<td class="mono" data-label={t('시간')}>{ts(log.started_at)}</td>
						<td data-label={t('상태')}><Badge variant={b.cls}>{t(b.label)}</Badge></td>
						<td class="url-cell" data-label="URL">
							{#if log.page_id}
								<a href={pagePath(log.page_site_id, log.page_id)} title={log.url}>{log.url}</a>
							{:else}
								<span title={log.url}>{log.url}</span>
							{/if}
						</td>
						<td class="num mono" data-label={t('소요')}>{log.duration_ms}ms</td>
						<td class="mono muted" data-label={t('출처')}>{log.source}</td>
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
	.more-link {
		font-size: 12px;
		margin-top: 8px;
	}
</style>
