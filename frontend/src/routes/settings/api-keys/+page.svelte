<script lang="ts">
	import { t } from '$lib/i18n';
	import { ts } from '$lib/format';
	import { api } from '$lib/api';
	import type { PersonalApiKeysData } from '$lib/types';
	import AlertBox from '$lib/components/AlertBox.svelte';
	import FormSection from '$lib/components/FormSection.svelte';
	import EmptyState from '$lib/components/EmptyState.svelte';
	import { createAction } from '$lib/action.svelte';
	import { Button } from '$lib/components/ui/button';
	import { Input } from '$lib/components/ui/input';

	let { data }: { data: { data: PersonalApiKeysData } } = $props();
	const d = $derived(data.data);

	const act = createAction();
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

	function create() {
		if (!name.trim()) return;
		newToken = '';
		return act.run(async () => {
			const r = await api<{ token: string }>('/settings/api-keys', {
				method: 'POST',
				body: JSON.stringify({
					name: name.trim(),
					can_view: canView && d.can_view,
					can_archive: canArchive && d.can_archive,
					expiry,
					custom_days: customDays
				})
			});
			newToken = r.token;
			name = '';
		});
	}

	function revoke(id: number) {
		if (!confirm(t('이 키를 폐기할까요?'))) return;
		return act.run(() => api(`/settings/api-keys/${id}/delete`, { method: 'POST' }));
	}
</script>

<h2>{t('개인 API Key')}</h2>
<p class="muted hint">
	{t('크롬 확장 등 본인 도구가 사용할 토큰입니다. 권한은 내 역할 범위 안에서만 부여됩니다.')}
</p>
<AlertBox error={act.error} />
{#if newToken}
	<div class="notice">
		{t('아래 키를 지금 복사하세요. 다시 표시되지 않습니다.')}
		<div class="mono token">{newToken}</div>
	</div>
{/if}

<FormSection title={t('새 키 발급')}>
	<div class="form">
		<Input type="text" bind:value={name} placeholder={t('키 이름')} class="flex-1 basis-[180px] min-w-0" />
		{#if d.can_view}<label class="opt"><input type="checkbox" bind:checked={canView} /> {t('보기')}</label>{/if}
		{#if d.can_archive}<label class="opt"><input type="checkbox" bind:checked={canArchive} /> {t('아카이브')}</label>{/if}
		<select bind:value={expiry}>
			{#each EXPIRY as [v, label]}<option value={v}>{t(label)}</option>{/each}
		</select>
		{#if expiry === 'custom'}
			<Input type="number" bind:value={customDays} min="1" max="3650" style="width:90px" />
		{/if}
		<Button onclick={create} disabled={act.busy || !name.trim()}>{t('발급')}</Button>
	</div>
</FormSection>

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
						<td><Button variant="destructive" onclick={() => revoke(k.id)} disabled={act.busy}>{t('폐기')}</Button></td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
{:else}
	<EmptyState message={t('발급된 개인 키가 없습니다.')} />
{/if}

<style>
	.hint {
		font-size: 12px;
		margin: 0 0 12px;
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
	}
	.form .opt {
		font-size: 13px;
		white-space: nowrap;
	}
</style>
