<script lang="ts">
	import { snapPath } from '$lib/urls';
	import { resolve } from '$app/paths';
	import { goto } from '$app/navigation';
	import { t } from '$lib/i18n';
	import { filesize, ts } from '$lib/format';
	import { api, ApiError } from '$lib/api';
	import { filterUrl } from '$lib/filters';
	import { createList } from '$lib/list.svelte';
	import type { PageTimeline } from '$lib/types';
	import AlertBox from '$lib/components/AlertBox.svelte';
	import EmptyState from '$lib/components/EmptyState.svelte';
	import Notes from '$lib/components/Notes.svelte';
	import Pager from '$lib/components/Pager.svelte';
	import PageSize from '$lib/components/PageSize.svelte';
	import { createAction } from '$lib/action.svelte';
	import { Badge, type BadgeVariant } from '$lib/components/ui/badge';
	import { Button } from '$lib/components/ui/button';

	let { data }: { data: { tl: PageTimeline } } = $props();
	const action = createAction();

	const FILTER_DEF = { limit: 25, page: 1 };
	const routeBase = () => `/archive/sites/${data.tl.site?.id}/page/${data.tl.page.id}`;
	const list = createList({
		source: () => data.tl,
		api: () => `/pages/${data.tl.page.id}`,
		route: routeBase,
		params: (d) => ({ limit: d.limit, page: d.page_num }),
		defaults: FILTER_DEF,
		onError: (m) => (action.error = m)
	});
	const tl = $derived(list.data);
	const snaps = $derived(tl.snapshots);
	const pageUrl = (n: number) => filterUrl(routeBase(), { limit: tl.limit, page: n }, FILTER_DEF);

	let force = $state(false);
	let interval = $state('86400');

	const INTERVALS: [string, string][] = [
		['3600', '1시간'],
		['21600', '6시간'],
		['86400', '1일'],
		['604800', '1주일'],
		['2592000', '1개월']
	];

	const rearchive = () =>
		action.run(() => api(`/pages/${tl.page.id}/rearchive`, { method: 'POST', body: JSON.stringify({ force }) }));
	const setSchedule = () =>
		action.run(() => api(`/pages/${tl.page.id}/schedule`, { method: 'POST', body: JSON.stringify({ interval }) }));
	const removeSchedule = () => action.run(() => api(`/pages/${tl.page.id}/schedule/delete`, { method: 'POST' }));

	async function deletePage() {
		const msg = tl.trash_enabled
			? t('이 페이지의 모든 스냅샷을 휴지통으로 옮길까요? 휴지통에서 복원할 수 있습니다.')
			: t('이 페이지의 모든 스냅샷을 삭제할까요? 되돌릴 수 없습니다.');
		if (!confirm(msg)) return;
		action.busy = true;
		action.error = '';
		try {
			await api(`/pages/${tl.page.id}/delete`, { method: 'POST' });
			goto(resolve('/archive/list'));
		} catch (err) {
			action.error = err instanceof ApiError ? err.message : String(err);
			action.busy = false;
		}
	}
</script>

<h2 class="mono page-url">{tl.page.url}</h2>
{#if tl.page.title}<p class="muted">{tl.page.title}</p>{/if}

<AlertBox error={action.error} />

<Notes
	kind="page"
	targetId={tl.page.id}
	notes={tl.notes}
	canView={tl.can_memo_view}
	canCreate={tl.can_memo_create}
	canDelete={tl.can_memo_delete}
/>

<div class="toolbar">
	{#if tl.site}<a href={resolve('/archive/sites/[id]', { id: String(tl.site.id) })}
			>← {t('사이트 상세')}</a
		>{/if}
	{#if tl.total >= 2}<a href={resolve('/diff/[id]', { id: String(tl.page.id) })}
			>{t('최신 2개 비교')}</a
		>{/if}
</div>

{#if tl.can_archive}
	<div class="actions">
		<div class="action-bar">
			<Button variant="outline" size="sm" onclick={rearchive} disabled={action.busy}>{t('재아카이빙')}</Button>
			<label class="muted"><input type="checkbox" bind:checked={force} /> {t('강제')}</label>
		</div>
		<div class="action-bar">
			{#if tl.schedule}
				<Badge variant="same">{t('스케줄')}: {tl.schedule.label}</Badge>
				<Button variant="outline" size="sm" onclick={removeSchedule} disabled={action.busy}>{t('스케줄 해제')}</Button>
			{:else}
				<select bind:value={interval}>
					{#each INTERVALS as [v, label] (v)}<option value={v}>{t(label)}</option>{/each}
				</select>
				<Button variant="outline" size="sm" onclick={setSchedule} disabled={action.busy}>{t('스케줄 등록')}</Button>
			{/if}
		</div>
	</div>
{/if}

<div class="hist-head">
	<h3>{t('스냅샷 이력')} ({tl.total})</h3>
	{#if tl.total > 0}
		<PageSize value={tl.limit} onchange={(n) => list.go({ limit: n, page: 1 })} />
	{/if}
</div>
{#if tl.total === 0}
	<EmptyState message={t('아직 스냅샷이 없습니다.')} />
{:else}
	<div class="table-wrap cards">
		<table>
			<thead>
				<tr>
					<th>#</th><th>{t('시간')}</th><th>{t('상태')}</th><th>{t('해시')}</th><th>{t('용량')}</th><th></th>
				</tr>
			</thead>
			<tbody>
				{#each snaps as item (item.snap.id)}
					<tr>
						<td class="num" data-label="#">{item.idx}</td>
						<td class="mono" data-label={t('시간')}>{ts(item.snap.taken_at)}</td>
						<td data-label={t('상태')}>
							<Badge variant={item.badge as BadgeVariant}>
								{item.badge === 'new' ? t('신규') : item.badge === 'changed' ? t('변경') : t('동일')}
							</Badge>
							{#if item.snap.origin === 'extension'}<Badge variant="same"
									>{t('브라우저 캡처')}</Badge
								>{/if}
							{#if item.snap.incomplete}<Badge variant="changed">{t('불완전')}</Badge>{/if}
						</td>
						<td class="mono muted" data-label={t('해시')}>{String(item.snap.content_hash).slice(0, 12)}</td>
						<td class="num mono" data-label={t('용량')}>{filesize(item.total_bytes)}</td>
						<td><a href={snapPath(tl.site?.id, tl.page.id, item.snap.id)}>{t('보기')}</a></td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
	<Pager
		page={tl.page_num}
		totalPages={tl.total_pages}
		href={pageUrl}
		onpage={(n) => list.go({ page: n })}
		busy={list.busy}
	/>
{/if}

{#if tl.can_delete}
	<fieldset class="danger-zone">
		<legend>{t('위험 구역')}</legend>
		<Button variant="destructive" onclick={deletePage} disabled={action.busy}>{t('이 페이지 삭제')}</Button>
	</fieldset>
{/if}

<style>
	.page-url {
		overflow-wrap: anywhere;
	}
	.hist-head {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 12px;
		flex-wrap: wrap;
	}
	.actions {
		display: flex;
		flex-direction: column;
		gap: 8px;
		margin: 12px 0;
	}
	.danger-zone {
		border: 1px solid var(--red);
		border-radius: 6px;
		margin-top: 28px;
		padding: 10px 14px 14px;
	}
	.danger-zone legend {
		color: var(--red);
		font-size: 12px;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		padding: 0 4px;
	}
</style>
