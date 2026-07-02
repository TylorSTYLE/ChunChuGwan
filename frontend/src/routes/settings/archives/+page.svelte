<script lang="ts">
	import { pagePath } from '$lib/urls';
	import { t } from '$lib/i18n';
	import { ts } from '$lib/format';
	import { filterUrl } from '$lib/filters';
	import { createList } from '$lib/list.svelte';
	import type { MyArchivesData } from '$lib/types';
	import Toolbar from '$lib/components/Toolbar.svelte';
	import Pager from '$lib/components/Pager.svelte';
	import EmptyState from '$lib/components/EmptyState.svelte';
	import AlertBox from '$lib/components/AlertBox.svelte';

	let { data }: { data: { data: MyArchivesData } } = $props();

	let listError = $state('');
	const ROUTE = '/settings/archives';
	const FILTER_DEF = { limit: 25, page: 1 };
	const list = createList({
		source: () => data.data,
		api: '/settings/archives',
		route: ROUTE,
		params: (d) => ({ status: d.status, limit: d.limit, page: d.page_num }),
		defaults: FILTER_DEF,
		onError: (m) => (listError = m)
	});
	const d = $derived(list.data);

	const STATUS_LABELS: Record<string, string> = {
		new: '새 스냅샷',
		changed: '변경됨',
		unchanged: '변경 없음',
		forced_same: '강제 저장',
		error: '오류'
	};

	const pageUrl = (n: number) =>
		filterUrl(ROUTE, { status: d.status, limit: d.limit, page: n }, FILTER_DEF);

	function fmtDuration(ms: number): string {
		if (!ms) return '';
		return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`;
	}
</script>

<h2>{t('내 아카이브')}</h2>
<AlertBox error={listError} />
<p class="muted hint">{t('내가 대시보드·확장에서 직접 요청한 단발 아카이빙 이력입니다.')}</p>

<Toolbar>
	<select value={d.status} onchange={(e) => list.go({ status: e.currentTarget.value, page: 1 })}>
		<option value="">{t('전체 상태')}</option>
		{#each d.statuses as s}<option value={s}>{t(STATUS_LABELS[s] ?? s)}</option>{/each}
	</select>
	<span class="spacer"></span>
	<span class="muted">{t('총')} {d.total}{t('건')}</span>
</Toolbar>

{#if d.items.length > 0}
	<div class="table-wrap">
		<table>
			<thead>
				<tr>
					<th>{t('시간')}</th>
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
								<a href={pagePath(log.page_site_id, log.page_id)}>{log.url}</a>
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
	<Pager
		page={d.page_num}
		totalPages={d.total_pages}
		href={pageUrl}
		onpage={(n) => list.go({ page: n })}
		busy={list.busy}
	/>
{:else}
	<EmptyState message={t('아직 요청한 아카이빙이 없습니다.')} />
{/if}

<style>
	/* 크기는 전역 .hint(app.css) — 페이지 설명 문단의 마진만 여기서 */
	.hint {
		margin: 0 0 12px;
	}
	td.url {
		word-break: break-all;
	}
</style>
