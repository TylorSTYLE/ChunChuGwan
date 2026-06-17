<script lang="ts">
	import { base } from '$app/paths';
	import { t } from '$lib/i18n';
	import { filesize, ts } from '$lib/format';
	import type { PageTimeline } from '$lib/types';

	let { data }: { data: { tl: PageTimeline } } = $props();
	const tl = $derived(data.tl);
	const snaps = $derived(tl.snapshots);
</script>

<h2 class="mono">{tl.page.url}</h2>
{#if tl.page.title}<p class="muted">{tl.page.title}</p>{/if}

<div class="toolbar">
	{#if tl.site}<a href="{base}/sites/{tl.site.id}">← {t('사이트 상세')}</a>{/if}
	{#if snaps.length >= 2}
		<a href="{base}/diff/{tl.page.id}">{t('최신 2개 비교')}</a>
	{/if}
	{#if tl.schedule}<span class="badge same">{t('스케줄')}: {tl.schedule.label}</span>{/if}
</div>

<h3>{t('스냅샷 이력')} ({snaps.length})</h3>
{#if snaps.length === 0}
	<p class="muted">{t('아직 스냅샷이 없습니다.')}</p>
{:else}
	<div class="table-wrap">
		<table>
			<thead>
				<tr>
					<th>#</th>
					<th>{t('시각')}</th>
					<th>{t('상태')}</th>
					<th>{t('해시')}</th>
					<th>{t('용량')}</th>
					<th></th>
				</tr>
			</thead>
			<tbody>
				{#each snaps as item}
					<tr>
						<td class="num">{item.idx}</td>
						<td class="mono">{ts(item.snap.taken_at)}</td>
						<td>
							<span class="badge {item.badge}"
								>{item.badge === 'new'
									? t('신규')
									: item.badge === 'changed'
										? t('변경')
										: t('동일')}</span
							>
						</td>
						<td class="mono muted">{String(item.snap.content_hash).slice(0, 12)}</td>
						<td class="num mono">{filesize(item.total_bytes)}</td>
						<td><a href="{base}/snapshot/{item.snap.id}">{t('보기')}</a></td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
{/if}
