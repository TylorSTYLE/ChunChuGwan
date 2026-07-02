<script lang="ts">
	import { t } from '$lib/i18n';
	import { ts } from '$lib/format';
	import { api } from '$lib/api';
	import { copyText } from '$lib/clipboard';
	import type { SystemApiKeysData } from '$lib/types';
	import AlertBox from '$lib/components/AlertBox.svelte';
	import FormSection from '$lib/components/FormSection.svelte';
	import EmptyState from '$lib/components/EmptyState.svelte';
	import { Button } from '$lib/components/ui/button';
	import { Input } from '$lib/components/ui/input';
	import { createAction } from '$lib/action.svelte';

	let { data }: { data: { data: SystemApiKeysData } } = $props();
	const d = $derived(data.data);

	const act = createAction();
	let newToken = $state('');
	// 1회 표시되는 새 키 복사 — 개인 API Key 화면과 동일 패턴(공통 clipboard 헬퍼).
	let copyState = $state('');
	let copyTimer: ReturnType<typeof setTimeout>;
	async function copyToken() {
		const ok = await copyText(newToken);
		copyState = ok ? 'ok' : 'fail';
		clearTimeout(copyTimer);
		copyTimer = setTimeout(() => (copyState = ''), 2000);
	}

	let name = $state('');
	let canView = $state(true);
	let canArchive = $state(false);
	let canClusterSend = $state(false);
	let canClusterReceive = $state(false);
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
			const r = await api<{ token: string }>('/system/api-keys', {
				method: 'POST',
				body: JSON.stringify({
					name: name.trim(),
					can_view: canView,
					can_archive: canArchive,
					can_cluster_send: canClusterSend,
					can_cluster_receive: canClusterReceive,
					expiry,
					custom_days: customDays
				})
			});
			newToken = r.token;
			name = '';
		});
	}

	function revoke(key: { id: number; name: string }) {
		if (!confirm(t("API 키 '{name}' 을 폐기할까요?").replace('{name}', key.name))) return;
		return act.run(() => api(`/system/api-keys/${key.id}/delete`, { method: 'POST' }));
	}
</script>

<h2>{t('API 키')}</h2>
<AlertBox error={act.error} />
{#if newToken}
	<div class="notice">
		{t('아래 키를 지금 복사하세요. 다시 표시되지 않습니다.')}
		<div class="mt-1.5 flex items-start gap-2">
			<span class="mono token flex-1 min-w-0">{newToken}</span>
			<Button
				variant={copyState === 'ok' ? 'default' : copyState === 'fail' ? 'destructive' : 'outline'}
				size="sm"
				class="shrink-0"
				onclick={copyToken}
			>
				{copyState === 'ok' ? t('복사됨') : copyState === 'fail' ? t('복사 실패') : t('복사')}
			</Button>
		</div>
	</div>
{/if}

<FormSection title={t('새 키 발급')}>
	<div class="form">
		<Input type="text" bind:value={name} placeholder={t('키 이름')} class="grow basis-[180px] min-w-0" />
		<label class="opt"><input type="checkbox" bind:checked={canView} /> {t('보기')}</label>
		<label class="opt"><input type="checkbox" bind:checked={canArchive} /> {t('아카이브')}</label>
		<label class="opt"><input type="checkbox" bind:checked={canClusterSend} /> {t('클러스터 보내기')}</label>
		<label class="opt"><input type="checkbox" bind:checked={canClusterReceive} /> {t('클러스터 받기')}</label>
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
	<div class="table-wrap cards">
		<table>
			<thead>
				<tr><th>{t('이름')}</th><th>{t('권한')}</th><th>{t('만료')}</th><th></th></tr>
			</thead>
			<tbody>
				{#each d.keys as k}
					<tr>
						<td data-label={t('이름')}>{k.name}</td>
						<td class="muted" data-label={t('권한')}>
							{[
								k.can_view ? t('보기') : '',
								k.can_archive ? t('아카이브') : '',
								k.can_cluster_send ? t('클러스터 보내기') : '',
								k.can_cluster_receive ? t('클러스터 받기') : ''
							]
								.filter(Boolean)
								.join(', ')}
						</td>
						<td class="mono" data-label={t('만료')}>{k.expires_at ? ts(k.expires_at) : t('영구')}</td>
						<td><Button variant="destructive" onclick={() => revoke(k)} disabled={act.busy}>{t('폐기')}</Button></td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
{:else}
	<EmptyState message={t('발급된 시스템 키가 없습니다.')} />
{/if}

<style>
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
	@media (max-width: 599px) {
		.form {
			flex-direction: column;
			align-items: stretch;
		}
		.form .opt {
			white-space: normal;
		}
	}
</style>
