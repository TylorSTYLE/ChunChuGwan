<script lang="ts">
	import { t } from '$lib/i18n';
	import { api } from '$lib/api';
	import type { SystemGroupsData, SystemGroup } from '$lib/types';
	import AlertBox from '$lib/components/AlertBox.svelte';
	import Field from '$lib/components/Field.svelte';
	import { createAction } from '$lib/action.svelte';
	import { Badge } from '$lib/components/ui/badge';
	import { Button } from '$lib/components/ui/button';
	import { Input } from '$lib/components/ui/input';

	let { data }: { data: { data: SystemGroupsData } } = $props();
	const d = $derived(data.data);

	const act = createAction();

	// 그룹별 편집 오버레이 — 사용자가 건드린 그룹만 담는다(없으면 서버값 사용).
	// 템플릿에서 상태를 변경하지 않도록 current() 는 순수 함수로 둔다.
	let overrides = $state<Record<string, { perms: Set<string>; label: string }>>({});

	function current(g: SystemGroup): { perms: Set<string>; label: string } {
		return overrides[g.name] ?? { perms: new Set(g.permissions), label: g.label };
	}

	// 커스텀 추가 폼
	let newName = $state('');
	let newLabel = $state('');
	let newPerms = $state<Set<string>>(new Set());

	function toggle(g: SystemGroup, p: string) {
		const c = current(g);
		const s = new Set(c.perms);
		if (s.has(p)) s.delete(p);
		else s.add(p);
		overrides = { ...overrides, [g.name]: { ...c, perms: s } };
	}
	function setLabel(g: SystemGroup, label: string) {
		overrides = { ...overrides, [g.name]: { ...current(g), label } };
	}
	const saveGroup = (g: SystemGroup) =>
		act.run(async () => {
			await api(`/system/groups/${g.name}`, {
				method: 'POST',
				body: JSON.stringify({
					label: current(g).label,
					permissions: [...current(g).perms]
				})
			});
			// 저장 성공 후 로컬 오버레이를 비운다 — 안 그러면 invalidateAll 로 갱신된
			// 서버 상태(다른 관리자의 변경·서버측 정규화)가 stale 오버레이에 계속 가린다.
			const { [g.name]: _removed, ...rest } = overrides;
			overrides = rest;
		});
	const deleteGroup = (g: SystemGroup) => {
		if (!confirm(t('이 권한 그룹을 삭제할까요?'))) return;
		return act.run(() => api(`/system/groups/${g.name}/delete`, { method: 'POST' }));
	};
	function toggleNew(p: string) {
		const s = new Set(newPerms);
		if (s.has(p)) s.delete(p);
		else s.add(p);
		newPerms = s;
	}
	const addGroup = () =>
		act.run(() =>
			api('/system/groups', {
				method: 'POST',
				body: JSON.stringify({ name: newName.trim(), label: newLabel.trim(), permissions: [...newPerms] })
			}).then(() => {
				newName = '';
				newLabel = '';
				newPerms = new Set();
			})
		);
</script>

<h2>{t('권한 그룹')}</h2>
<AlertBox error={act.error} />

{#each d.groups as g}
	{@const e = current(g)}
	<fieldset class="group">
		<legend>
			{g.label} <span class="mono muted">{g.name}</span>
			{#if g.is_builtin}<Badge variant="same">{t('기본')}</Badge>{/if}
			<span class="muted">· {g.member_count}{t('명')}</span>
		</legend>
		<div class="stack">
			{#if !g.is_builtin}
				<Field label={t('표시 라벨')}>
					<Input type="text" value={e.label} oninput={(ev) => setLabel(g, ev.currentTarget.value)} />
				</Field>
			{/if}
			<div class="perms">
				{#each d.permissions_catalog as p}
					<label><input type="checkbox" checked={e.perms.has(p)} onchange={() => toggle(g, p)} /> {d.permission_labels[p] ?? p}</label>
				{/each}
			</div>
			<div class="action-bar">
				<Button onclick={() => saveGroup(g)} disabled={act.busy}>{t('저장')}</Button>
				{#if !g.is_builtin && g.member_count === 0}
					<Button variant="destructive" onclick={() => deleteGroup(g)} disabled={act.busy}>{t('삭제')}</Button>
				{/if}
			</div>
		</div>
	</fieldset>
{/each}

<h3>{t('커스텀 그룹 추가')}</h3>
<fieldset class="group">
	<div class="stack">
		<div class="form-grid">
			<Field label={t('이름(영문/숫자/_)')}><Input type="text" bind:value={newName} /></Field>
			<Field label={t('표시 라벨')}><Input type="text" bind:value={newLabel} /></Field>
		</div>
		<div class="perms">
			{#each d.permissions_catalog as p}
				<label><input type="checkbox" checked={newPerms.has(p)} onchange={() => toggleNew(p)} /> {d.permission_labels[p] ?? p}</label>
			{/each}
		</div>
		<Button onclick={addGroup} disabled={act.busy || !newName.trim()}>{t('그룹 추가')}</Button>
	</div>
</fieldset>

<style>
	.group {
		border: 1px solid var(--border);
		border-radius: 6px;
		margin-bottom: 14px;
		padding: 10px 14px;
	}
	.group legend {
		font-size: 13px;
		padding: 0 4px;
	}
	.stack {
		display: flex;
		flex-direction: column;
		gap: 12px;
		margin-top: 6px;
	}
	.perms {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(min(100%, 170px), 1fr));
		gap: 4px 10px;
		font-size: 12px;
	}
	.perms label {
		display: flex;
		align-items: center;
		gap: 6px;
	}
</style>
