<script lang="ts">
	import { pagePath, snapPath } from '$lib/urls';
	import { t } from '$lib/i18n';
	import { ts } from '$lib/format';
	import { api } from '$lib/api';
	import { filterUrl } from '$lib/filters';
	import { createList } from '$lib/list.svelte';
	import type { LogsData } from '$lib/types';
	import AlertBox from '$lib/components/AlertBox.svelte';
	import Toolbar from '$lib/components/Toolbar.svelte';
	import Pager from '$lib/components/Pager.svelte';
	import EmptyState from '$lib/components/EmptyState.svelte';
	import { createAction } from '$lib/action.svelte';

	let { data }: { data: { logs: LogsData } } = $props();
	const act = createAction();

	const ROUTE = '/log/archive';
	const FILTER_DEF = { limit: 25, page: 1 };
	const list = createList({
		source: () => data.logs,
		api: '/logs',
		route: ROUTE,
		params: (d) => ({ domain: d.domain, status: d.status, limit: d.limit, page: d.page_num }),
		defaults: FILTER_DEF,
		onError: (m) => (act.error = m)
	});
	const d = $derived(list.data);

	const retry = (logId: number) =>
		act.run(
			() => api(`/logs/${logId}/retry`, { method: 'POST' }),
			t('재시도가 등록되었습니다 — 백그라운드에서 진행됩니다.')
		);

	const STATUS_LABEL: Record<string, string> = {
		new: '신규',
		changed: '변경',
		unchanged: '동일',
		forced_same: '동일(강제)',
		error: '실패'
	};
	const BADGE: Record<string, string> = {
		new: 'new',
		changed: 'changed',
		unchanged: 'same',
		forced_same: 'same',
		error: 'error'
	};

	const applyFilter = (patch: Record<string, string>) => list.go({ ...patch, page: 1 });
	const pageUrl = (n: number) =>
		filterUrl(ROUTE, { domain: d.domain, status: d.status, limit: d.limit, page: n }, FILTER_DEF);
</script>

<h2>{t('아카이빙 로그')}</h2>
<AlertBox error={act.error} notice={act.notice} />

<Toolbar>
	<select value={d.domain} onchange={(e) => applyFilter({ domain: e.currentTarget.value })}>
		<option value="">{t('전체 도메인')}</option>
		{#each d.domains as dom}<option value={dom}>{dom}</option>{/each}
	</select>
	<select value={d.status} onchange={(e) => applyFilter({ status: e.currentTarget.value })}>
		<option value="">{t('전체 상태')}</option>
		{#each d.statuses as st}<option value={st}>{t(STATUS_LABEL[st] ?? st)}</option>{/each}
	</select>
	<span class="spacer"></span>
	<span class="muted">{t('총')} {d.total}{t('건')}</span>
</Toolbar>

{#if d.items.length === 0}
	<EmptyState message={t('로그가 없습니다.')} />
{:else}
	<div class="table-wrap wide">
		<table>
			<thead>
				<tr>
					<th>{t('시간')}</th>
					<th>{t('상태')}</th>
					<th>URL</th>
					<th>{t('소요')}</th>
					<th>{t('출처')}</th>
					<th></th>
				</tr>
			</thead>
			<tbody>
				{#each d.items as it}
					<tr>
						<td class="mono">{ts(it.log.started_at)}</td>
						<td>
							<span class="badge {BADGE[it.log.status] ?? 'same'}">{t(STATUS_LABEL[it.log.status] ?? it.log.status)}</span>
						</td>
						<td class="url-cell">
							{#if it.log.page_id}
								<a href={pagePath(it.log.page_site_id, it.log.page_id)} title={it.log.url}>{it.log.url}</a>
							{:else}
								<span title={it.log.url}>{it.log.url}</span>
							{/if}
						</td>
						<td class="num mono">{it.log.duration_ms}ms</td>
						<td class="mono muted">{it.log.source}</td>
						<td>
							{#if it.log.snapshot_id}
								<a href={snapPath(it.log.page_site_id, it.log.page_id, it.log.snapshot_id)}>{t('보기')}</a>
							{:else if d.can_archive && it.log.status === 'error'}
								<button type="button" class="linkbtn" onclick={() => retry(it.log.id)} disabled={act.busy}>{t('재시도')}</button>
							{/if}
						</td>
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
	/* 첫 컬럼(시간)은 한 줄 유지 — 폭이 좁아 줄바꿈되던 문제 보정 */
	th:first-child,
	td.mono:first-child {
		white-space: nowrap;
	}
	/* 재시도: '보기' 링크와 같은 텍스트 링크 모양 (버튼 박스 제거) */
	.linkbtn {
		background: none;
		border: none;
		padding: 0;
		color: var(--link);
		cursor: pointer;
		font: inherit;
	}
	.linkbtn:hover {
		text-decoration: underline;
	}
	.linkbtn:disabled {
		color: var(--muted);
		cursor: default;
		text-decoration: none;
	}
</style>
