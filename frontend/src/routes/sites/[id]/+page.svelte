<script lang="ts">
	import { base } from '$app/paths';
	import { t } from '$lib/i18n';
	import { filesize, ts } from '$lib/format';
	import type { SiteDetail } from '$lib/types';

	let { data }: { data: { site: SiteDetail } } = $props();
	const s = $derived(data.site);
</script>

<h2 class="mono">{s.site.site_key}</h2>
{#if s.site_title}<p class="muted">{s.site_title}</p>{/if}

<div class="stat-grid">
	<div class="stat-card">
		<div class="label">{t('페이지')}</div>
		<div class="value">{s.page_count}</div>
	</div>
	<div class="stat-card">
		<div class="label">{t('스냅샷')}</div>
		<div class="value">{s.snapshot_total}</div>
	</div>
	<div class="stat-card">
		<div class="label">{t('문서')}</div>
		<div class="value">{s.doc_total}</div>
	</div>
	<div class="stat-card">
		<div class="label">{t('용량')}</div>
		<div class="value">{filesize(s.site_bytes)}</div>
	</div>
</div>

<h3>{t('페이지')} ({s.page_count})</h3>
<div class="table-wrap">
	<table>
		<thead>
			<tr><th>URL</th><th>{t('스냅샷')}</th><th>{t('용량')}</th><th>{t('마지막')}</th></tr>
		</thead>
		<tbody>
			{#each s.pages as p}
				<tr>
					<td class="url-cell"><a href="{base}/page/{p.id}" title={p.url}>{p.url}</a></td>
					<td class="num">{p.snapshot_count ?? '-'}</td>
					<td class="num mono">{filesize(p.bytes)}</td>
					<td class="mono">{p.last_snapshot_at ? ts(String(p.last_snapshot_at)) : '-'}</td>
				</tr>
			{/each}
		</tbody>
	</table>
</div>
{#if s.pager.total_pages > 1}
	<div class="pager">
		{#if s.pager.page > 1}
			<a href="{base}/sites/{s.site.id}?page={s.pager.page - 1}">← {t('이전')}</a>
		{/if}
		<span class="muted">{s.pager.page} / {s.pager.total_pages}</span>
		{#if s.pager.page < s.pager.total_pages}
			<a href="{base}/sites/{s.site.id}?page={s.pager.page + 1}">{t('다음')} →</a>
		{/if}
	</div>
{/if}

{#if s.crawls.length > 0}
	<h3>{t('사이트 아카이브 회차')} ({s.crawls.length})</h3>
	<div class="table-wrap">
		<table>
			<thead><tr><th>{t('시작')}</th><th>{t('상태')}</th><th>{t('완료/실패/대기')}</th></tr></thead>
			<tbody>
				{#each s.crawls as c}
					<tr>
						<td class="mono">{ts(String(c.started_at))}</td>
						<td>{String(c.status)}</td>
						<td class="num mono">{c.done_count}/{c.failed_count}/{c.pending_count}</td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
{/if}

{#if s.schedules.length > 0 || s.crawl_schedules.length > 0}
	<h3>{t('스케줄')}</h3>
	<ul class="muted">
		{#each s.schedules as sc}
			<li><a href="{base}/page/{sc.page_id}">{t('페이지')} #{sc.page_id}</a> — {sc.label}</li>
		{/each}
		{#each s.crawl_schedules as cs}
			<li class="mono">{cs.start_url} — {cs.label}</li>
		{/each}
	</ul>
{/if}

{#if s.failed_items.length > 0}
	<h3>{t('실패한 작업')} ({s.failed_items.length})</h3>
	<div class="table-wrap">
		<table>
			<thead><tr><th>{t('시각')}</th><th>URL</th><th>{t('오류')}</th></tr></thead>
			<tbody>
				{#each s.failed_items as f}
					<tr>
						<td class="mono">{f.at ? ts(String(f.at)) : '-'}</td>
						<td class="url-cell">{f.url}</td>
						<td class="muted">{f.error}</td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
{/if}

<style>
	td.url-cell {
		max-width: 420px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.pager {
		display: flex;
		gap: 12px;
		align-items: center;
		margin-top: 10px;
		font-size: 13px;
	}
</style>
