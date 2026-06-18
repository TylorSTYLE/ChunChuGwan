<script lang="ts">
	import { invalidateAll } from '$app/navigation';
	import { t } from '$lib/i18n';
	import { api, ApiError } from '$lib/api';
	import type { SystemGroupsData, SystemGroup } from '$lib/types';

	let { data }: { data: { data: SystemGroupsData } } = $props();
	const d = $derived(data.data);

	let error = $state('');
	let busy = $state(false);

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
		run(() =>
			api(`/system/groups/${g.name}`, {
				method: 'POST',
				body: JSON.stringify({
					label: current(g).label,
					permissions: [...current(g).perms]
				})
			})
		);
	const deleteGroup = (g: SystemGroup) => {
		if (!confirm(t('이 권한 그룹을 삭제할까요?'))) return;
		return run(() => api(`/system/groups/${g.name}/delete`, { method: 'POST' }));
	};
	function toggleNew(p: string) {
		const s = new Set(newPerms);
		if (s.has(p)) s.delete(p);
		else s.add(p);
		newPerms = s;
	}
	const addGroup = () =>
		run(() =>
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
{#if error}<div class="error">{error}</div>{/if}

{#each d.groups as g}
	{@const e = current(g)}
	<fieldset class="group">
		<legend>
			{g.label} <span class="mono muted">{g.name}</span>
			{#if g.is_builtin}<span class="badge same">{t('기본')}</span>{/if}
			<span class="muted">· {g.member_count}{t('명')}</span>
		</legend>
		{#if !g.is_builtin}
			<label class="lbl">{t('표시 라벨')}
				<input type="text" value={e.label} oninput={(ev) => setLabel(g, ev.currentTarget.value)} />
			</label>
		{/if}
		<div class="perms">
			{#each d.permissions_catalog as p}
				<label><input type="checkbox" checked={e.perms.has(p)} onchange={() => toggle(g, p)} /> {d.permission_labels[p] ?? p}</label>
			{/each}
		</div>
		<div class="row">
			<button onclick={() => saveGroup(g)} disabled={busy}>{t('저장')}</button>
			{#if !g.is_builtin && g.member_count === 0}
				<button class="danger" onclick={() => deleteGroup(g)} disabled={busy}>{t('삭제')}</button>
			{/if}
		</div>
	</fieldset>
{/each}

<h3>{t('커스텀 그룹 추가')}</h3>
<fieldset class="group">
	<div class="row">
		<input type="text" bind:value={newName} placeholder={t('이름(영문/숫자/_)')} />
		<input type="text" bind:value={newLabel} placeholder={t('표시 라벨')} />
	</div>
	<div class="perms">
		{#each d.permissions_catalog as p}
			<label><input type="checkbox" checked={newPerms.has(p)} onchange={() => toggleNew(p)} /> {d.permission_labels[p] ?? p}</label>
		{/each}
	</div>
	<button onclick={addGroup} disabled={busy || !newName.trim()}>{t('그룹 추가')}</button>
</fieldset>

<style>
	.error {
		background: var(--red-bg);
		color: var(--red-text);
		border-radius: 4px;
		padding: 8px 12px;
		margin-bottom: 12px;
		font-size: 13px;
	}
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
	.lbl {
		display: block;
		font-size: 12px;
		margin-bottom: 8px;
	}
	.perms {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
		gap: 3px;
		font-size: 12px;
		margin-bottom: 8px;
	}
	.row {
		display: flex;
		gap: 8px;
	}
	button.danger {
		color: #fff;
		background: var(--red);
		border-color: var(--red);
	}
</style>
