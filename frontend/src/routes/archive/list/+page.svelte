<script lang="ts">
	import { onMount } from 'svelte';
	import { base } from '$app/paths';
	import { invalidateAll } from '$app/navigation';
	import { t } from '$lib/i18n';
	import { filesize, ts } from '$lib/format';
	import { api } from '$lib/api';
	import { filterUrl } from '$lib/filters';
	import { createList } from '$lib/list.svelte';
	import type { SitesData } from '$lib/types';
	import Toolbar from '$lib/components/Toolbar.svelte';
	import Pager from '$lib/components/Pager.svelte';
	import PageSize from '$lib/components/PageSize.svelte';
	import EmptyState from '$lib/components/EmptyState.svelte';
	import { Badge } from '$lib/components/ui/badge';
	import { Input } from '$lib/components/ui/input';

	let { data }: { data: { sites: SitesData } } = $props();

	const ROUTE = '/archive/list';
	const FILTER_DEF = { limit: 25, page: 1 };
	// 필터(q)·페이지는 서버에서 전체 사이트 대상으로 적용한다 — 현재 페이지 클라이언트 필터가 아님.
	const list = createList({
		source: () => data.sites,
		api: '/sites',
		route: ROUTE,
		params: (d) => ({ q: d.q, limit: d.limit, page: d.page_num }),
		defaults: FILTER_DEF
	});
	const d = $derived(list.data);

	const pageUrl = (n: number) => filterUrl(ROUTE, { q: d.q, limit: d.limit, page: n }, FILTER_DEF);

	// 진행 중 아카이빙·사람 확인 대기 집합이 바뀌면 현재 페이지/필터를 다시 불러 상태를 갱신한다
	// (createList load 가 URL 의 q/page/limit 을 읽으므로 보고 있던 페이지·필터가 유지된다).
	type Active = { active: string[]; needs_human?: { id: number; url: string }[] };
	onMount(() => {
		let last = '';
		const timer = setInterval(async () => {
			if (typeof document !== 'undefined' && document.hidden) return;
			// 페이저/필터 요청(list.go)이 진행 중이면 invalidateAll 을 건너뛴다 — load 재실행이
			// go() 와 경쟁해 URL(최신 params)과 표시 데이터가 어긋나는 것을 막는다. last 를
			// 갱신하지 않으므로 go() 가 끝난 뒤 다음 틱에서 변화를 정상 반영한다.
			if (list.busy) return;
			try {
				const a = await api<Active>('/active');
				const key = JSON.stringify([a.active, (a.needs_human ?? []).map((j) => j.url)]);
				if (last && key !== last) await invalidateAll();
				last = key;
			} catch {
				/* 일시 오류 — 다음 폴링에서 회복 */
			}
		}, 5000);
		return () => clearInterval(timer);
	});
</script>

<h2>{t('아카이브 사이트 목록')}</h2>

<Toolbar>
	<Input
		type="search"
		value={d.q}
		placeholder={t('사이트 검색')}
		onchange={(e) => list.go({ q: e.currentTarget.value, page: 1 })}
	/>
	<span class="spacer"></span>
	<span class="muted">{t('총')} {d.total}{t('건')}</span>
	<PageSize value={d.limit} onchange={(n) => list.go({ limit: n, page: 1 })} />
</Toolbar>

{#if d.items.length === 0}
	<EmptyState message={d.q ? t('검색 결과가 없습니다.') : t('아직 아카이브가 없습니다.')} />
{:else}
	<div class="table-wrap wide cards">
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
				{#each d.items as s}
					<tr>
						<td data-label={t('사이트')}>
							{#if s.site_id}
								<a href="{base}/archive/sites/{s.site_id}">{s.site_key}</a>
							{:else}
								<span>{s.site_key}</span>
							{/if}
							{#if s.crawling}<Badge variant="new">{t('아카이빙 중')}</Badge>{/if}
							{#if s.title}<div class="muted">{s.title}</div>{/if}
						</td>
						<td class="num" data-label={t('페이지')}>{s.page_count}</td>
						<td class="num" data-label={t('스냅샷')}>{s.snapshot_count}</td>
						<td class="num" data-label={t('스케줄')}>{s.schedule_count || '-'}</td>
						<td class="num mono" data-label={t('용량')}>{filesize(s.bytes)}</td>
						<td class="mono activity" data-label={t('마지막 활동')}>{s.activity_at ? ts(s.activity_at) : '-'}</td>
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
{/if}

<style>
	/* 마지막 활동(시간) 컬럼은 한 줄 유지 — 폭이 좁아 줄바꿈되던 문제 보정 */
	td.activity,
	th:last-child {
		white-space: nowrap;
		min-width: 160px;
	}
</style>
