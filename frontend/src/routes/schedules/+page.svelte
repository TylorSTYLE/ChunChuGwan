<script lang="ts">
	import { base } from '$app/paths';
	import { t } from '$lib/i18n';
	import { ts } from '$lib/format';
	import type { SchedulesData } from '$lib/types';

	let { data }: { data: { sched: SchedulesData } } = $props();
	const s = $derived(data.sched);
</script>

<h2>{t('스케줄')}</h2>

<h3>{t('페이지 재아카이빙')} ({s.items.length})</h3>
{#if s.items.length === 0}
	<p class="muted">{t('등록된 스케줄이 없습니다.')}</p>
{:else}
	<div class="table-wrap">
		<table>
			<thead>
				<tr><th>URL</th><th>{t('주기')}</th><th>{t('다음 실행')}</th></tr>
			</thead>
			<tbody>
				{#each s.items as item}
					<tr>
						<td class="url-cell"><a href="{base}/page/{item.page_id}">{item.url}</a></td>
						<td>{item.label}</td>
						<td class="mono">{item.next_run_at ? ts(String(item.next_run_at)) : '-'}</td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
{/if}

{#if s.crawl_items.length > 0}
	<h3>{t('사이트 재아카이빙')} ({s.crawl_items.length})</h3>
	<div class="table-wrap">
		<table>
			<thead>
				<tr><th>{t('시작 URL')}</th><th>{t('주기')}</th><th>{t('다음 실행')}</th></tr>
			</thead>
			<tbody>
				{#each s.crawl_items as item}
					<tr>
						<td class="url-cell mono">{item.start_url}</td>
						<td>{item.label}</td>
						<td class="mono">{item.next_run_at ? ts(String(item.next_run_at)) : '-'}</td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
{/if}

<style>
	td.url-cell {
		max-width: 480px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
</style>
