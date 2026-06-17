<script lang="ts">
	import { invalidateAll } from '$app/navigation';
	import { t } from '$lib/i18n';
	import { api, ApiError } from '$lib/api';
	import type { SystemUsersData, SystemUser } from '$lib/types';

	let { data }: { data: { data: SystemUsersData } } = $props();
	const d = $derived(data.data);

	let error = $state('');
	let notice = $state('');
	let busy = $state(false);

	// 권한 편집 펼침 상태 (user_id → 편집 중 권한 set)
	let editing = $state<number | null>(null);
	let editPerms = $state<Set<string>>(new Set());
	let deleteEmail = $state<Record<number, string>>({});

	// 초대 폼
	let inviteEmail = $state('');
	let inviteRole = $state('viewer');
	let inviteLink = $state('');

	async function run(fn: () => Promise<unknown>, ok = '') {
		busy = true;
		error = '';
		notice = '';
		try {
			await fn();
			if (ok) notice = ok;
			await invalidateAll();
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
		} finally {
			busy = false;
		}
	}

	const setRole = (u: SystemUser, role: string) =>
		run(() =>
			api(`/system/users/${u.id}/role`, { method: 'POST', body: JSON.stringify({ role }) })
		);
	const forceLogout = (u: SystemUser) =>
		run(
			() => api(`/system/users/${u.id}/logout`, { method: 'POST' }),
			t('세션을 로그아웃했습니다.')
		);

	function startEdit(u: SystemUser) {
		editing = u.id;
		editPerms = new Set(d.user_perms[u.id]?.effective ?? []);
	}
	function togglePerm(p: string) {
		const s = new Set(editPerms);
		if (s.has(p)) s.delete(p);
		else s.add(p);
		editPerms = s;
	}
	const savePerms = (u: SystemUser) =>
		run(() =>
			api(`/system/users/${u.id}/permissions`, {
				method: 'POST',
				body: JSON.stringify({ permissions: [...editPerms] })
			}).then(() => {
				editing = null;
			})
		);

	const deleteUser = (u: SystemUser) =>
		run(() =>
			api(`/system/users/${u.id}/delete`, {
				method: 'POST',
				body: JSON.stringify({ email: deleteEmail[u.id] ?? '' })
			})
		);

	async function invite() {
		if (!inviteEmail.trim()) return;
		busy = true;
		error = '';
		notice = '';
		inviteLink = '';
		try {
			const r = await api<{ link: string; mailed: boolean }>('/system/users/invite', {
				method: 'POST',
				body: JSON.stringify({ email: inviteEmail.trim(), role: inviteRole })
			});
			notice = r.mailed ? t('초대 메일을 보냈습니다.') : t('초대 링크를 직접 전달하세요.');
			if (!r.mailed) inviteLink = r.link;
			inviteEmail = '';
			await invalidateAll();
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
		} finally {
			busy = false;
		}
	}
	const cancelInvite = (id: number) =>
		run(() => api(`/system/users/invite/${id}/delete`, { method: 'POST' }));
</script>

<h2>{t('사용자')}</h2>

{#if error}<div class="error">{error}</div>{/if}
{#if notice}<div class="notice">{notice}</div>{/if}

<div class="table-wrap wide">
	<table>
		<thead>
			<tr><th>{t('이메일')}</th><th>{t('역할')}</th><th>{t('권한')}</th><th></th></tr>
		</thead>
		<tbody>
			{#each d.users as u}
				<tr>
					<td>
						{u.email}
						{#if u.display_name}<span class="muted"> · {u.display_name}</span>{/if}
						{#if u.is_founder}<span class="badge same">{t('최초 관리자')}</span>{/if}
					</td>
					<td>
						{#if u.is_founder}
							<span class="badge">{d.role_labels[u.role] ?? u.role}</span>
						{:else}
							<select
								value={u.role}
								disabled={busy}
								onchange={(e) => setRole(u, e.currentTarget.value)}
							>
								{#each d.roles as r}<option value={r}>{d.role_labels[r] ?? r}</option>{/each}
								{#if !d.roles.includes(u.role)}<option value={u.role}
										>{d.role_labels[u.role] ?? u.role}</option
									>{/if}
							</select>
						{/if}
					</td>
					<td>
						{#if editing === u.id}
							<div class="perms">
								{#each d.permissions_catalog as p}
									<label
										><input
											type="checkbox"
											checked={editPerms.has(p)}
											onchange={() => togglePerm(p)}
										/> {d.permission_labels[p] ?? p}</label
									>
								{/each}
								<div>
									<button onclick={() => savePerms(u)} disabled={busy}>{t('저장')}</button>
									<button onclick={() => (editing = null)}>{t('취소')}</button>
								</div>
							</div>
						{:else}
							<span class="muted">{(d.user_perms[u.id]?.effective ?? []).length} {t('개')}</span>
							{#if d.permission_roles.includes(u.role) && !u.is_founder}
								<button onclick={() => startEdit(u)}>{t('편집')}</button>
							{/if}
						{/if}
					</td>
					<td>
						{#if !u.is_founder && u.id !== d.me_id}
							<button onclick={() => forceLogout(u)} disabled={busy}>{t('로그아웃')}</button>
							<details>
								<summary class="muted">{t('삭제')}</summary>
								<input
									type="text"
									placeholder={t('확인 이메일')}
									value={deleteEmail[u.id] ?? ''}
									oninput={(e) => (deleteEmail = { ...deleteEmail, [u.id]: e.currentTarget.value })}
								/>
								<button class="danger" onclick={() => deleteUser(u)} disabled={busy}>{t('삭제')}</button>
							</details>
						{/if}
					</td>
				</tr>
			{/each}
		</tbody>
	</table>
</div>

<h3>{t('초대')}</h3>
{#if !d.mail_enabled}<p class="muted">{t('SMTP 미설정 — 초대 링크를 직접 전달합니다.')}</p>{/if}
<div class="toolbar">
	<input type="email" bind:value={inviteEmail} placeholder={t('이메일')} />
	<select bind:value={inviteRole}>
		{#each d.invitable_roles as r}<option value={r}>{d.role_labels[r] ?? r}</option>{/each}
	</select>
	<button onclick={invite} disabled={busy}>{t('초대')}</button>
</div>
{#if inviteLink}<div class="notice mono">{inviteLink}</div>{/if}

{#if d.invites.length > 0}
	<table>
		<thead><tr><th>{t('이메일')}</th><th>{t('역할')}</th><th></th></tr></thead>
		<tbody>
			{#each d.invites as inv}
				<tr>
					<td>{inv.email}</td>
					<td>{d.role_labels[inv.role] ?? inv.role}</td>
					<td><button onclick={() => cancelInvite(inv.id)} disabled={busy}>{t('취소')}</button></td>
				</tr>
			{/each}
		</tbody>
	</table>
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
	.perms {
		display: flex;
		flex-direction: column;
		gap: 3px;
		font-size: 12px;
	}
	button.danger {
		color: #fff;
		background: var(--red);
		border-color: var(--red);
	}
</style>
