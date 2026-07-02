<script lang="ts">
	import { pagePath } from '$lib/urls';
	import { t } from '$lib/i18n';
	import { ts } from '$lib/format';
	import type { SchedulesData } from '$lib/types';
	import EmptyState from '$lib/components/EmptyState.svelte';

	let { data }: { data: { sched: SchedulesData } } = $props();
	const s = $derived(data.sched);
</script>

<h2>{t('스케줄')}</h2>

<h3>{t('페이지 재아카이빙')} ({s.items.length})</h3>
{#if s.items.length === 0}
	<EmptyState message={t('등록된 스케줄이 없습니다.')} />
{:else}
	<div class="table-wrap cards">
		<table>
			<thead>
				<tr><th>URL</th><th>{t('주기')}</th><th>{t('다음 실행')}</th></tr>
			</thead>
			<tbody>
				{#each s.items as item (item.id)}
					<tr>
						<td class="url-cell" data-label="URL"><a href={pagePath(item.site_id, item.page_id)}>{item.url}</a></td>
						<td data-label={t('주기')}>{item.label}</td>
						<td class="mono" data-label={t('다음 실행')}>{item.next_run_at ? ts(String(item.next_run_at)) : '-'}</td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
{/if}

{#if s.crawl_items.length > 0}
	<h3>{t('사이트 재아카이빙')} ({s.crawl_items.length})</h3>
	<div class="table-wrap cards">
		<table>
			<thead>
				<tr><th>{t('시작 URL')}</th><th>{t('주기')}</th><th>{t('다음 실행')}</th></tr>
			</thead>
			<tbody>
				{#each s.crawl_items as item (item.id)}
					<tr>
						<td class="url-cell mono" data-label={t('시작 URL')}>{item.start_url}</td>
						<td data-label={t('주기')}>{item.label}</td>
						<td class="mono" data-label={t('다음 실행')}>{item.next_run_at ? ts(String(item.next_run_at)) : '-'}</td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
{/if}
