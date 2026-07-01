<script lang="ts">
	import { invalidateAll } from '$app/navigation';
	import { t } from '$lib/i18n';
	import { ts } from '$lib/format';
	import { api } from '$lib/api';
	import type { ClusterData } from '$lib/types';
	import AlertBox from '$lib/components/AlertBox.svelte';
	import FormSection from '$lib/components/FormSection.svelte';
	import EmptyState from '$lib/components/EmptyState.svelte';
	import Toggle from '$lib/components/Toggle.svelte';
	import { Button } from '$lib/components/ui/button';
	import { Input } from '$lib/components/ui/input';
	import { Badge } from '$lib/components/ui/badge';
	import { createAction } from '$lib/action.svelte';

	let { data }: { data: { data: ClusterData } } = $props();
	const d = $derived(data.data);

	const act = createAction();

	// 노드 신원
	let displayName = $state('');
	// 동기화 설정
	let syncInterval = $state(300);
	let protectDefault = $state(true);
	// 피어 등록 폼
	let baseUrl = $state('');
	let apiKey = $state('');
	let peerSend = $state(false);
	let peerReceive = $state(false);

	// 최초 1회만 서버값으로 초기화한다 — 매 data 변경마다 재동기화하면 피어 토글 등의
	// invalidateAll 이 입력 중이던 노드 이름·조정 주기 편집을 되돌린다(general 과 같은 취지).
	let syncedFromServer = false;
	$effect(() => {
		if (syncedFromServer) return;
		syncedFromServer = true;
		displayName = d.node.display_name;
		syncInterval = d.sync_interval_seconds;
		protectDefault = d.protect_default;
	});

	const STATUS_BADGE: Record<string, 'new' | 'running' | 'changed' | 'error' | 'same'> = {
		active: 'new',
		pending: 'running',
		degraded: 'changed',
		error: 'error',
		revoked: 'error'
	};
	const STATUS_LABEL: Record<string, string> = {
		active: '연결됨',
		pending: '대기',
		degraded: '일시 오류',
		error: '오류',
		revoked: '폐기됨'
	};

	function saveNode() {
		return act.run(async () => {
			await api('/system/cluster/node', {
				method: 'POST',
				body: JSON.stringify({ display_name: displayName.trim() })
			});
			await invalidateAll();
		});
	}

	function saveSync() {
		return act.run(async () => {
			await api('/system/cluster/sync-settings', {
				method: 'POST',
				body: JSON.stringify({
					sync_interval_seconds: syncInterval,
					protect_default: protectDefault
				})
			});
			await invalidateAll();
		});
	}

	function addPeer() {
		if (!baseUrl.trim() || !apiKey.trim()) return;
		return act.run(async () => {
			await api('/system/cluster/peers', {
				method: 'POST',
				body: JSON.stringify({
					base_url: baseUrl.trim(),
					api_key: apiKey.trim(),
					send_enabled: peerSend,
					receive_enabled: peerReceive
				})
			});
			baseUrl = '';
			apiKey = '';
			peerSend = false;
			peerReceive = false;
			await invalidateAll();
		});
	}

	function toggleDir(id: number, send: boolean, receive: boolean) {
		return act.run(async () => {
			await api(`/system/cluster/peers/${id}`, {
				method: 'POST',
				body: JSON.stringify({ send_enabled: send, receive_enabled: receive })
			});
			await invalidateAll();
		});
	}

	function removePeer(id: number) {
		if (!confirm(t('이 피어 연결을 해제할까요? 받은 아카이브는 보존됩니다.'))) return;
		return act.run(async () => {
			await api(`/system/cluster/peers/${id}/delete`, { method: 'POST' });
			await invalidateAll();
		});
	}
</script>

<h2>{t('클러스터')}</h2>
<p class="muted intro">
	{t(
		'여러 춘추관 인스턴스를 연결해 아카이브를 선택적으로 주고받습니다. 연결은 항상 이쪽에서 개시합니다(보내기=push, 받기=pull).'
	)}
</p>
<AlertBox error={act.error} />

{#if !d.secret_configured}
	<AlertBox
		warn={t(
			'WCCG_SECRET_KEY 가 설정되지 않아 피어 키를 안전하게 저장할 수 없습니다. 환경변수를 설정한 뒤 피어를 등록하세요.'
		)}
	/>
{/if}

<FormSection title={t('이 노드')}>
	<div class="grid">
		<span class="lbl">{t('노드 식별자 (UUID)')}</span>
		<div class="mono muted">{d.node.node_id}</div>
		<label class="lbl" for="cl-name">{t('표시 이름')}</label>
		<div class="row">
			<Input id="cl-name" type="text" bind:value={displayName} placeholder={t('예: 집 NAS')} class="grow" />
			<Button onclick={saveNode} disabled={act.busy}>{t('저장')}</Button>
		</div>
		<div class="hint muted">{t('표시 전용입니다 — 피어 식별·신뢰는 항상 UUID 로 합니다.')}</div>
	</div>
</FormSection>

<FormSection title={t('동기화 설정')}>
	<div class="grid">
		<label class="lbl" for="cl-int">{t('조정 주기 (초)')}</label>
		<div class="row">
			<Input
				id="cl-int"
				type="number"
				bind:value={syncInterval}
				min={d.sync_interval_min}
				max={d.sync_interval_max}
				style="width:140px"
			/>
			<span class="hint muted">{t('{min}~{max}초').replace('{min}', String(d.sync_interval_min)).replace('{max}', String(d.sync_interval_max))}</span>
		</div>
		<div class="span2">
			<Toggle
				bind:checked={protectDefault}
				label={t('기본 보호 (다른 클러스터로 보내지 않음)')}
				description={t('사이트·아카이브에 보호 설정이 없을 때 적용되는 시스템 기본값입니다.')}
			/>
		</div>
		<div class="span2"><Button onclick={saveSync} disabled={act.busy}>{t('저장')}</Button></div>
	</div>
</FormSection>

<FormSection title={t('피어 연결 추가')}>
	<div class="grid">
		<label class="lbl" for="cl-url">{t('피어 주소')}</label>
		<Input id="cl-url" type="url" bind:value={baseUrl} placeholder="https://peer.example" />
		<label class="lbl" for="cl-key">{t('피어 발급 시스템 키')}</label>
		<Input id="cl-key" type="password" bind:value={apiKey} placeholder="wccg_..." autocomplete="off" />
		<div class="span2 dirs">
			<label class="opt"><input type="checkbox" bind:checked={peerSend} /> {t('보내기 (이 피어로 push)')}</label>
			<label class="opt"><input type="checkbox" bind:checked={peerReceive} /> {t('받기 (이 피어에서 pull)')}</label>
		</div>
		<div class="span2">
			<Button
				onclick={addPeer}
				disabled={act.busy || !d.secret_configured || !baseUrl.trim() || !apiKey.trim()}
				>{t('연결')}</Button
			>
		</div>
	</div>
</FormSection>

{#if d.peers.length > 0}
	<div class="table-wrap cards">
		<table>
			<thead>
				<tr>
					<th>{t('피어')}</th>
					<th>{t('방향')}</th>
					<th>{t('상태')}</th>
					<th>{t('마지막 동기화')}</th>
					<th></th>
				</tr>
			</thead>
			<tbody>
				{#each d.peers as p}
					<tr>
						<td data-label={t('피어')}>
							<div>{p.display_name || t('(이름 없음)')}</div>
							<div class="mono muted url">{p.base_url}</div>
						</td>
						<td data-label={t('방향')}>
							<div class="dirtoggles">
								<label class="opt">
									<input
										type="checkbox"
										checked={p.send_enabled}
										disabled={act.busy}
										onchange={(e) => toggleDir(p.id, e.currentTarget.checked, p.receive_enabled)}
									/>
									{t('보내기')}
								</label>
								<label class="opt">
									<input
										type="checkbox"
										checked={p.receive_enabled}
										disabled={act.busy}
										onchange={(e) => toggleDir(p.id, p.send_enabled, e.currentTarget.checked)}
									/>
									{t('받기')}
								</label>
							</div>
						</td>
						<td data-label={t('상태')}>
							<Badge variant={STATUS_BADGE[p.status] ?? 'same'}>{t(STATUS_LABEL[p.status] ?? p.status)}</Badge>
							{#if p.last_error}<div class="err muted">{p.last_error}</div>{/if}
						</td>
						<td class="mono" data-label={t('마지막 동기화')}>{p.last_synced_at ? ts(p.last_synced_at) : '—'}</td>
						<td>
							<Button variant="destructive" onclick={() => removePeer(p.id)} disabled={act.busy}
								>{t('해제')}</Button
							>
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
{:else}
	<EmptyState message={t('연결된 피어가 없습니다.')} />
{/if}

<style>
	.intro {
		font-size: 13px;
		margin-bottom: 12px;
	}
	.grid {
		display: grid;
		grid-template-columns: max-content 1fr;
		gap: 10px 14px;
		align-items: center;
	}
	.grid .lbl {
		font-size: 13px;
		white-space: nowrap;
	}
	.grid .span2 {
		grid-column: 1 / -1;
	}
	.row {
		display: flex;
		gap: 8px;
		align-items: center;
	}
	.hint {
		font-size: 12px;
	}
	.dirs,
	.dirtoggles {
		display: flex;
		gap: 14px;
		flex-wrap: wrap;
	}
	.opt {
		font-size: 13px;
		white-space: nowrap;
	}
	.url,
	.err {
		font-size: 12px;
		word-break: break-all;
	}
	@media (max-width: 599px) {
		.grid {
			grid-template-columns: 1fr;
		}
	}
</style>
