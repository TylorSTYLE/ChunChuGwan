<script lang="ts">
	import { base } from '$app/paths';
	import { t } from '$lib/i18n';
	import { filesize, ts } from '$lib/format';
	import type { SiteItem } from '$lib/types';

	let { data }: { data: { sites: SiteItem[] } } = $props();
	const sites = $derived(data.sites);
</script>

<h2>{t('아카이브 사이트 목록')}</h2>

{#if sites.length === 0}
	<p class="muted">{t('아직 아카이브가 없습니다.')}</p>
{:else}
	<div class="table-wrap wide">
		<table>
			<thead>
				<tr>
					<th>{t('사이트')}</th>
					<th>{t('페이지')}</th>
					<th>{t('스냅샷')}</th>
					<th>{t('스케줄')}</th>
					<th>{t('용량')}</th>
					<th>{t('마지막 활동')}</th>
				</tr>
			</thead>
			<tbody>
				{#each sites as s}
					<tr>
						<td>
							{#if s.site_id}
								<a href="{base}/sites/{s.site_id}">{s.site_key}</a>
							{:else}
								<span>{s.site_key}</span>
							{/if}
							{#if s.crawling}<span class="badge new">{t('아카이빙 중')}</span>{/if}
							{#if s.title}<div class="muted">{s.title}</div>{/if}
						</td>
						<td class="num">{s.page_count}</td>
						<td class="num">{s.snapshot_count}</td>
						<td class="num">{s.schedule_count || '-'}</td>
						<td class="num mono">{filesize(s.bytes)}</td>
						<td class="mono">{s.activity_at ? ts(s.activity_at) : '-'}</td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
{/if}
