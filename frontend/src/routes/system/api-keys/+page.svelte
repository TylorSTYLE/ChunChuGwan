<script lang="ts">
	import { invalidateAll } from '$app/navigation';
	import { t } from '$lib/i18n';
	import { ts } from '$lib/format';
	import { api, ApiError } from '$lib/api';
	import type { SystemApiKeysData } from '$lib/types';

	let { data }: { data: { data: SystemApiKeysData } } = $props();
	const d = $derived(data.data);

	let error = $state('');
	let busy = $state(false);
	let newToken = $state('');

	let name = $state('');
	let canView = $state(true);
	let canArchive = $state(false);
	let expiry = $state('permanent');
	let customDays = $state(30);

	const EXPIRY: [string, string][] = [
		['permanent', '영구'],
		['1d', '1일'],
		['1m', '1개월 (30일)'],
		['1y', '1년 (365일)'],
		['custom', '사용자 지정 (일)']
	];

	async function create() {
		if (!name.trim()) return;
		busy = true;
		error = '';
		newToken = '';
		try {
			const r = await api<{ token: string }>('/system/api-keys', {
				method: 'POST',
				body: JSON.stringify({
					name: name.trim(),
					can_view: canView,
					can_archive: canArchive,
					expiry,
					custom_days: customDays
				})
			});
			newToken = r.token;
			name = '';
			await invalidateAll();
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
		} finally {
			busy = false;
		}
	}

	async function revoke(id: number) {
		if (!confirm(t('이 키를 폐기할까요?'))) return;
		busy = true;
		error = '';
		try {
			await api(`/system/api-keys/${id}/delete`, { method: 'POST' });
			await invalidateAll();
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
		} finally {
			busy = false;
		}
	}
</script>

<h2>{t('API 키')}</h2>
{#if error}<div class="error">{error}</div>{/if}
{#if newToken}
	<div class="notice">
		{t('아래 키를 지금 복사하세요. 다시 표시되지 않습니다.')}
		<div class="mono token">{newToken}</div>
	</div>
{/if}

<h3>{t('새 키 발급')}</h3>
<div class="form">
	<input type="text" bind:value={name} placeholder={t('키 이름')} />
	<label><input type="checkbox" bind:checked={canView} /> {t('보기')}</label>
	<label><input type="checkbox" bind:checked={canArchive} /> {t('아카이브')}</label>
	<select bind:value={expiry}>
		{#each EXPIRY as [v, label]}<option value={v}>{t(label)}</option>{/each}
	</select>
	{#if expiry === 'custom'}
		<input type="number" bind:value={customDays} min="1" max="3650" style="width:90px" />
	{/if}
	<button onclick={create} disabled={busy || !name.trim()}>{t('발급')}</button>
</div>

{#if d.keys.length > 0}
	<div class="table-wrap">
		<table>
			<thead>
				<tr><th>{t('이름')}</th><th>{t('권한')}</th><th>{t('만료')}</th><th></th></tr>
			</thead>
			<tbody>
				{#each d.keys as k}
					<tr>
						<td>{k.name}</td>
						<td class="muted">
							{[k.can_view ? t('보기') : '', k.can_archive ? t('아카이브') : '']
								.filter(Boolean)
								.join(', ')}
						</td>
						<td class="mono">{k.expires_at ? ts(k.expires_at) : t('영구')}</td>
						<td><button class="danger" onclick={() => revoke(k.id)} disabled={busy}>{t('폐기')}</button></td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
{:else}
	<p class="muted">{t('발급된 시스템 키가 없습니다.')}</p>
{/if}

<style>
	.error {
		background: var(--red-bg);
		color: var(--red-text);
		border-radius: 4px;
		padding: 8px 12px;
		margin-bottom: 12px;
		font-size: 13px;
	}
	.token {
		margin-top: 6px;
		word-break: break-all;
		font-size: 13px;
	}
	.form {
		display: flex;
		gap: 8px;
		align-items: center;
		flex-wrap: wrap;
		margin-bottom: 16px;
	}
	button.danger {
		color: #fff;
		background: var(--red);
		border-color: var(--red);
	}
</style>
