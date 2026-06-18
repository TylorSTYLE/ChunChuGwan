<script lang="ts">
	import { base } from '$app/paths';
	import { t } from '$lib/i18n';
	import { filesize, ts } from '$lib/format';
	import type { DocumentsData } from '$lib/types';

	let { data }: { data: { docs: DocumentsData } } = $props();
	const d = $derived(data.docs);
</script>

<h2>{t('전체 문서(파일)')}</h2>

{#if d.legacy_pending}
	<div class="notice-box muted">
		{t('구형 스냅샷의 문서가 남아 있습니다. compact 를 실행하면 통합 목록에 반영됩니다.')}
	</div>
{/if}

<div class="stat-grid">
	<div class="stat-card">
		<div class="label">{t('문서 그룹')}</div>
		<div class="value">{d.totals.groups ?? 0}</div>
	</div>
	<div class="stat-card">
		<div class="label">{t('고유 용량')}</div>
		<div class="value">{filesize(d.totals.unique_bytes)}</div>
	</div>
	<div class="stat-card">
		<div class="label">{t('절감 용량')}</div>
		<div class="value">{filesize(d.totals.saved_bytes)}</div>
	</div>
</div>

{#if d.groups.length === 0}
	<p class="muted">{t('문서가 없습니다.')}</p>
{:else}
	<div class="table-wrap wide">
		<table>
			<thead>
				<tr>
					<th>{t('문서명')}</th>
					<th>{t('용량')}</th>
					<th>{t('페이지')}</th>
					<th>{t('참조')}</th>
					<th>{t('마지막')}</th>
				</tr>
			</thead>
			<tbody>
				{#each d.groups as g}
					<tr>
						<td><a href="/document/{String(g.sha256)}/{g.file}" download>{g.file}</a></td>
						<td class="num mono">{filesize(g.bytes)}</td>
						<td class="url-cell"><a href="{base}/page/{g.page_id}">{g.page_url}</a></td>
						<td class="num">{g.snapshot_count}</td>
						<td class="mono">{g.last_seen ? ts(String(g.last_seen)) : '-'}</td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
	{#if d.page > 1 || d.has_next}
		<div class="pager">
			{#if d.page > 1}<a href="{base}/documents?page={d.page - 1}">← {t('이전')}</a>{/if}
			<span class="muted">{d.page}</span>
			{#if d.has_next}<a href="{base}/documents?page={d.page + 1}">{t('다음')} →</a>{/if}
		</div>
	{/if}
{/if}

<style>
	.notice-box {
		background: var(--amber-bg);
		color: var(--amber);
		border-radius: 4px;
		padding: 8px 12px;
		margin-bottom: 12px;
		font-size: 13px;
	}
	td.url-cell {
		max-width: 360px;
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
