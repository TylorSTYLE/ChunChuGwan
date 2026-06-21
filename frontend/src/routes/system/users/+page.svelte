<script lang="ts">
	import { t } from '$lib/i18n';
	import { api } from '$lib/api';
	import { filterUrl } from '$lib/filters';
	import { createList } from '$lib/list.svelte';
	import type { SystemUsersData, SystemUser } from '$lib/types';
	import AlertBox from '$lib/components/AlertBox.svelte';
	import Toolbar from '$lib/components/Toolbar.svelte';
	import Pager from '$lib/components/Pager.svelte';
	import PageSize from '$lib/components/PageSize.svelte';
	import { createAction } from '$lib/action.svelte';
	import { Badge } from '$lib/components/ui/badge';
	import { Button } from '$lib/components/ui/button';
	import { Input } from '$lib/components/ui/input';

	let { data }: { data: { data: SystemUsersData } } = $props();
	const act = createAction();

	// 사용자 목록만 페이징한다(초대는 전체). 액션 후 invalidateAll → 현재 페이지로 reseed.
	const ROUTE = '/system/users';
	const FILTER_DEF = { limit: 25, page: 1 };
	const list = createList({
		source: () => data.data,
		api: '/system/users',
		route: ROUTE,
		params: (d) => ({ limit: d.limit, page: d.page_num }),
		defaults: FILTER_DEF,
		onError: (m) => (act.error = m)
	});
	const d = $derived(list.data);
	const pageUrl = (n: number) => filterUrl(ROUTE, { limit: d.limit, page: n }, FILTER_DEF);

	// 표시이름 편집 (user_id → 입력값). 권한은 역할 단위로만 부여한다(세분 권한 편집 없음).
	let nameEdit = $state<Record<number, string>>({});
	let deleteEmail = $state<Record<number, string>>({});

	// 초대 폼
	let inviteEmail = $state('');
	let inviteRole = $state('viewer');
	let inviteLink = $state('');

	const setRole = (u: SystemUser, role: string) =>
		act.run(() => api(`/system/users/${u.id}/role`, { method: 'POST', body: JSON.stringify({ role }) }));
	const forceLogout = (u: SystemUser) =>
		act.run(() => api(`/system/users/${u.id}/logout`, { method: 'POST' }), t('세션을 로그아웃했습니다.'));

	const saveName = (u: SystemUser) =>
		act.run(
			() =>
				api(`/system/users/${u.id}/name`, {
					method: 'POST',
					body: JSON.stringify({ display_name: nameEdit[u.id] ?? '' })
				}),
			t('표시이름을 저장했습니다.')
		);

	const deleteUser = (u: SystemUser) =>
		act.run(() =>
			api(`/system/users/${u.id}/delete`, {
				method: 'POST',
				body: JSON.stringify({ email: deleteEmail[u.id] ?? '' })
			})
		);

	function invite() {
		if (!inviteEmail.trim()) return;
		inviteLink = '';
		return act.run(async () => {
			const r = await api<{ link: string; mailed: boolean }>('/system/users/invite', {
				method: 'POST',
				body: JSON.stringify({ email: inviteEmail.trim(), role: inviteRole })
			});
			act.notice = r.mailed ? t('초대 메일을 보냈습니다.') : t('초대 링크를 직접 전달하세요.');
			if (!r.mailed) inviteLink = r.link;
			inviteEmail = '';
		});
	}
	const cancelInvite = (id: number) =>
		act.run(() => api(`/system/users/invite/${id}/delete`, { method: 'POST' }));
</script>

<h2>{t('사용자')}</h2>

<AlertBox error={act.error} notice={act.notice} />

<Toolbar>
	<span class="spacer"></span>
	<span class="muted">{t('총')} {d.total}{t('건')}</span>
	<PageSize value={d.limit} onchange={(n) => list.go({ limit: n, page: 1 })} />
</Toolbar>

<div class="table-wrap wide">
	<table>
		<thead>
			<tr><th>{t('이메일')}</th><th>{t('표시이름')}</th><th>{t('역할')}</th><th></th></tr>
		</thead>
		<tbody>
			{#each d.users as u}
				<tr>
					<td>
						{u.email}
						{#if u.is_founder}<Badge variant="same">{t('최초 관리자')}</Badge>{/if}
					</td>
					<td>
						<div class="name-edit">
							<Input
								type="text"
								class="w-36 max-w-full"
								placeholder="-"
								value={nameEdit[u.id] ?? u.display_name ?? ''}
								oninput={(e) => (nameEdit = { ...nameEdit, [u.id]: e.currentTarget.value })}
							/>
							<Button variant="outline" size="sm" onclick={() => saveName(u)} disabled={act.busy}>{t('저장')}</Button>
						</div>
					</td>
					<td>
						{#if u.is_founder}
							<span class="badge">{d.role_labels[u.role] ?? u.role}</span>
						{:else}
							<select value={u.role} disabled={act.busy} onchange={(e) => setRole(u, e.currentTarget.value)}>
								{#each d.roles as r}<option value={r}>{d.role_labels[r] ?? r}</option>{/each}
								{#if !d.roles.includes(u.role)}<option value={u.role}>{d.role_labels[u.role] ?? u.role}</option>{/if}
							</select>
						{/if}
					</td>
					<td>
						{#if !u.is_founder && u.id !== d.me_id}
							<div class="action-bar">
								<Button variant="outline" size="sm" onclick={() => forceLogout(u)} disabled={act.busy}>{t('로그아웃')}</Button>
								<details>
									<summary class="muted">{t('삭제')}</summary>
									<div class="del-confirm">
										<Input
											type="text"
											placeholder={t('확인 이메일')}
											value={deleteEmail[u.id] ?? ''}
											oninput={(e) => (deleteEmail = { ...deleteEmail, [u.id]: e.currentTarget.value })}
										/>
										<Button variant="destructive" onclick={() => deleteUser(u)} disabled={act.busy}>{t('삭제')}</Button>
									</div>
								</details>
							</div>
						{/if}
					</td>
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

<h3>{t('초대')}</h3>
{#if !d.mail_enabled}<p class="muted">{t('SMTP 미설정 — 초대 링크를 직접 전달합니다.')}</p>{/if}
<Toolbar>
	<Input type="email" bind:value={inviteEmail} placeholder={t('이메일')} />
	<select bind:value={inviteRole}>
		{#each d.invitable_roles as r}<option value={r}>{d.role_labels[r] ?? r}</option>{/each}
	</select>
	<Button onclick={invite} disabled={act.busy}>{t('초대')}</Button>
</Toolbar>
{#if inviteLink}<div class="notice mono link-out">{inviteLink}</div>{/if}

{#if d.invites.length > 0}
	<div class="table-wrap">
		<table>
			<thead><tr><th>{t('이메일')}</th><th>{t('역할')}</th><th></th></tr></thead>
			<tbody>
				{#each d.invites as inv}
					<tr>
						<td>{inv.email}</td>
						<td>{d.role_labels[inv.role] ?? inv.role}</td>
						<td><Button variant="outline" size="sm" onclick={() => cancelInvite(inv.id)} disabled={act.busy}>{t('취소')}</Button></td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
{/if}

<style>
	.name-edit {
		display: flex;
		gap: 6px;
		align-items: center;
	}
	.del-confirm {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
		margin-top: 6px;
	}
	.link-out {
		word-break: break-all;
	}
</style>
