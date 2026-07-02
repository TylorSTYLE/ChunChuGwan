<script lang="ts">
	import { pagePath } from '$lib/urls';
	import { filterUrl } from '$lib/filters';
	import { createList } from '$lib/list.svelte';
	import { t } from '$lib/i18n';
	import { filesize, ts } from '$lib/format';
	import type { DocumentsData } from '$lib/types';
	import EmptyState from '$lib/components/EmptyState.svelte';
	import StatGrid from '$lib/components/StatGrid.svelte';
	import StatCard from '$lib/components/StatCard.svelte';
	import AlertBox from '$lib/components/AlertBox.svelte';

	let { data }: { data: { docs: DocumentsData } } = $props();

	let listError = $state('');
	const ROUTE = '/archive/documents';
	const FILTER_DEF = { page: 1 };
	const list = createList({
		source: () => data.docs,
		api: '/documents',
		route: ROUTE,
		params: (d) => ({ page: d.page }),
		defaults: FILTER_DEF,
		onError: (m) => (listError = m)
	});
	const d = $derived(list.data);

	const pageUrl = (n: number) => filterUrl(ROUTE, { page: n }, FILTER_DEF);
	// Pager 컴포넌트는 total_pages 가 필요 — 문서 목록은 has_next 커서식이라 인라인 페이저를
	// 직접 두고, 좌클릭만 가로채 in-place 갱신한다(수식키·새 탭은 href 로 실제 이동).
	function nav(e: MouseEvent, n: number) {
		if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
		e.preventDefault();
		if (list.busy) return;
		list.go({ page: n });
	}
</script>

<h2>{t('전체 문서(파일)')}</h2>
<AlertBox error={listError} />

{#if d.legacy_pending}
	<div class="warn">
		{t('구형 스냅샷의 문서가 남아 있습니다. compact 를 실행하면 통합 목록에 반영됩니다.')}
	</div>
{/if}

<StatGrid>
	<StatCard label={t('문서 그룹')} value={d.totals.groups ?? 0} />
	<StatCard label={t('고유 용량')} value={filesize(d.totals.unique_bytes)} />
	<StatCard label={t('절감 용량')} value={filesize(d.totals.saved_bytes)} />
</StatGrid>

{#if d.groups.length === 0}
	<EmptyState message={t('문서가 없습니다.')} />
	{#if d.page > 1}
		<!-- 범위 초과 page 딥링크(빈 결과)에서 되돌아갈 수단 — 서버 클램프가 없어 필요 -->
		<nav class="pager">
			<a href={pageUrl(1)} onclick={(e) => nav(e, 1)}>← {t('첫 페이지로')}</a>
		</nav>
	{/if}
{:else}
	<div class="table-wrap wide cards">
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
						<td data-label={t('문서명')}><a href="/document/{String(g.sha256)}/{g.file}" download>{g.file}</a></td>
						<td class="num mono" data-label={t('용량')}>{filesize(g.bytes)}</td>
						<td class="url-cell" data-label={t('페이지')}><a href={pagePath(g.site_id, g.page_id)} title={g.page_url}>{g.page_url}</a></td>
						<td class="num" data-label={t('참조')}>{g.snapshot_count}</td>
						<td class="mono" data-label={t('마지막')}>{g.last_seen ? ts(String(g.last_seen)) : '-'}</td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
	{#if d.page > 1 || d.has_next}
		<nav class="pager" aria-busy={list.busy}>
			{#if d.page > 1}<a href={pageUrl(d.page - 1)} onclick={(e) => nav(e, d.page - 1)}>← {t('이전')}</a
				>{/if}
			<span class="muted">{d.page}</span>
			{#if d.has_next}<a href={pageUrl(d.page + 1)} onclick={(e) => nav(e, d.page + 1)}>{t('다음')} →</a
				>{/if}
		</nav>
	{/if}
{/if}
