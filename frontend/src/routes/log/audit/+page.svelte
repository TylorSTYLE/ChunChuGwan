<script lang="ts">
	import { t } from '$lib/i18n';
	import { ts } from '$lib/format';
	import { filterUrl } from '$lib/filters';
	import { createList } from '$lib/list.svelte';
	import type { AuditLogsData } from '$lib/types';
	import Toolbar from '$lib/components/Toolbar.svelte';
	import Pager from '$lib/components/Pager.svelte';
	import PageSize from '$lib/components/PageSize.svelte';
	import EmptyState from '$lib/components/EmptyState.svelte';
	import AlertBox from '$lib/components/AlertBox.svelte';
	import { Badge, type BadgeVariant } from '$lib/components/ui/badge';

	let { data }: { data: { audit: AuditLogsData } } = $props();

	let listError = $state('');
	const ROUTE = '/log/audit';
	const FILTER_DEF = { limit: 25, page: 1 };
	const list = createList({
		source: () => data.audit,
		api: '/audit',
		route: ROUTE,
		params: (d) => ({ action: d.action, actor: d.actor, limit: d.limit, page: d.page_num }),
		defaults: FILTER_DEF,
		onError: (m) => (listError = m)
	});
	const d = $derived(list.data);

	const ACTION_BADGE: Record<string, BadgeVariant> = {
		archive: 'new',
		view: 'running',
		download: 'changed',
		admin: 'same'
	};

	const applyFilter = (patch: Record<string, string>) => list.go({ ...patch, page: 1 });
	const pageUrl = (n: number) =>
		filterUrl(ROUTE, { action: d.action, actor: d.actor, limit: d.limit, page: n }, FILTER_DEF);

	function actionLabel(a: string): string {
		return d.action_labels[a] ?? a;
	}
</script>

<h2>{t('감사 로그')}</h2>
<AlertBox error={listError} />
<p class="muted lead">{t('누가 아카이빙·열람·문서 다운로드·관리 작업을 했는지 기록')}</p>

<Toolbar>
	<select value={d.action} onchange={(e) => applyFilter({ action: e.currentTarget.value })}>
		<option value="">{t('모든 종류')}</option>
		{#each d.actions as a (a)}<option value={a}>{actionLabel(a)}</option>{/each}
	</select>
	<select value={d.actor} onchange={(e) => applyFilter({ actor: e.currentTarget.value })}>
		<option value="">{t('모든 요청자')}</option>
		{#each d.actors as a (a)}<option value={a}>{a}</option>{/each}
	</select>
	<span class="spacer"></span>
	<span class="muted">{t('총')} {d.total}{t('건')}</span>
	<PageSize value={d.limit} options={d.limits} onchange={(n) => list.go({ limit: n, page: 1 })} />
</Toolbar>

{#if d.logs.length === 0}
	<EmptyState message={t('감사 기록이 없습니다.')} />
{:else}
	<div class="table-wrap wide cards">
		<table>
			<thead>
				<tr>
					<th class="col-time">{t('시간')}</th>
					<th>{t('종류')}</th>
					<th>{t('요청자')}</th>
					<th>{t('대상')}</th>
					<th>{t('내용')}</th>
				</tr>
			</thead>
			<tbody>
				{#each d.logs as log (log.id)}
					<tr>
						<td class="mono col-time" data-label={t('시간')}>{ts(log.created_at)}</td>
						<td data-label={t('종류')}><Badge variant={ACTION_BADGE[log.action] ?? 'same'}>{actionLabel(log.action)}</Badge></td>
						<td class="mono" data-label={t('요청자')}>{log.actor}</td>
						<td class="mono muted target" data-label={t('대상')}>{log.target ?? '-'}</td>
						<td data-label={t('내용')}>{log.message}</td>
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
	.lead {
		font-size: 13px;
		margin: -6px 0 12px;
	}
	.col-time {
		white-space: nowrap;
		min-width: 160px;
	}
	.target {
		max-width: 280px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
</style>
