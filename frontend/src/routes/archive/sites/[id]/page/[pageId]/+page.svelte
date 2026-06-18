<script lang="ts">
	import { snapPath } from '$lib/urls';
	import { base } from '$app/paths';
	import { goto, invalidateAll } from '$app/navigation';
	import { t } from '$lib/i18n';
	import { filesize, ts } from '$lib/format';
	import { api, ApiError } from '$lib/api';
	import type { PageTimeline } from '$lib/types';

	let { data }: { data: { tl: PageTimeline } } = $props();
	const tl = $derived(data.tl);
	const snaps = $derived(tl.snapshots);

	let force = $state(false);
	let interval = $state('86400');
	let busy = $state(false);
	let error = $state('');

	const INTERVALS: [string, string][] = [
		['3600', '1시간'],
		['21600', '6시간'],
		['86400', '1일'],
		['604800', '1주일'],
		['2592000', '1개월']
	];

	async function run(fn: () => Promise<unknown>) {
		busy = true;
		error = '';
		try {
			await fn();
			await invalidateAll();
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
		} finally {
			busy = false;
		}
	}

	const rearchive = () =>
		run(() =>
			api(`/pages/${tl.page.id}/rearchive`, { method: 'POST', body: JSON.stringify({ force }) })
		);
	const setSchedule = () =>
		run(() =>
			api(`/pages/${tl.page.id}/schedule`, {
				method: 'POST',
				body: JSON.stringify({ interval })
			})
		);
	const removeSchedule = () =>
		run(() => api(`/pages/${tl.page.id}/schedule/delete`, { method: 'POST' }));

	async function deletePage() {
		if (!confirm(t('이 페이지의 모든 스냅샷을 삭제할까요? 되돌릴 수 없습니다.'))) return;
		busy = true;
		error = '';
		try {
			await api(`/pages/${tl.page.id}/delete`, { method: 'POST' });
			goto(`${base}/archive/list`);
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
			busy = false;
		}
	}
</script>

<h2 class="mono">{tl.page.url}</h2>
{#if tl.page.title}<p class="muted">{tl.page.title}</p>{/if}

{#if error}<div class="error">{error}</div>{/if}

<div class="toolbar">
	{#if tl.site}<a href="{base}/archive/sites/{tl.site.id}">← {t('사이트 상세')}</a>{/if}
	{#if snaps.length >= 2}<a href="{base}/diff/{tl.page.id}">{t('최신 2개 비교')}</a>{/if}
</div>

{#if tl.can_archive}
	<div class="actions">
		<div class="action-row">
			<button onclick={rearchive} disabled={busy}>{t('재아카이빙')}</button>
			<label class="muted"><input type="checkbox" bind:checked={force} /> {t('강제')}</label>
		</div>
		<div class="action-row">
			{#if tl.schedule}
				<span class="badge same">{t('스케줄')}: {tl.schedule.label}</span>
				<button onclick={removeSchedule} disabled={busy}>{t('스케줄 해제')}</button>
			{:else}
				<select bind:value={interval}>
					{#each INTERVALS as [v, label]}<option value={v}>{t(label)}</option>{/each}
				</select>
				<button onclick={setSchedule} disabled={busy}>{t('스케줄 등록')}</button>
			{/if}
		</div>
	</div>
{/if}

<h3>{t('스냅샷 이력')} ({snaps.length})</h3>
{#if snaps.length === 0}
	<p class="muted">{t('아직 스냅샷이 없습니다.')}</p>
{:else}
	<div class="table-wrap">
		<table>
			<thead>
				<tr>
					<th>#</th><th>{t('시간')}</th><th>{t('상태')}</th><th>{t('해시')}</th><th>{t('용량')}</th
					><th></th>
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
						<td><a href={snapPath(tl.site?.id, tl.page.id, item.snap.id)}>{t('보기')}</a></td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
{/if}

{#if tl.can_delete}
	<fieldset class="danger-zone">
		<legend>{t('위험 구역')}</legend>
		<button class="danger" onclick={deletePage} disabled={busy}>{t('이 페이지 삭제')}</button>
	</fieldset>
{/if}

<style>
	.actions {
		display: flex;
		flex-direction: column;
		gap: 8px;
		margin: 12px 0;
	}
	.action-row {
		display: flex;
		gap: 8px;
		align-items: center;
	}
	.error {
		background: var(--red-bg);
		color: var(--red-text);
		border-radius: 4px;
		padding: 8px 12px;
		margin-bottom: 12px;
		font-size: 13px;
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
	button.danger {
		color: #fff;
		background: var(--red);
		border-color: var(--red);
	}
	button.danger:hover {
		background: var(--red-hover);
	}
</style>
