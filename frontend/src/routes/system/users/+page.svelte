<script lang="ts">
	import { t } from '$lib/i18n';
	import { api } from '$lib/api';
	import { ts } from '$lib/format';
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

	// 초대 폼 — 메일 미설정 시 직접 전달할 링크와 그 대상 이메일을 함께 노출한다
	// (재생성도 같은 자리에 표시하므로 어느 초대의 링크인지 이메일로 식별).
	let inviteEmail = $state('');
	let inviteRole = $state('viewer');
	let inviteLink = $state('');
	let inviteLinkEmail = $state('');

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
		inviteLinkEmail = '';
		return act.run(async () => {
			const r = await api<{ email: string; link: string; mailed: boolean }>('/system/users/invite', {
				method: 'POST',
				body: JSON.stringify({ email: inviteEmail.trim(), role: inviteRole })
			});
			act.notice = r.mailed ? t('초대 메일을 보냈습니다.') : t('초대 링크를 직접 전달하세요.');
			if (!r.mailed) {
				inviteLink = r.link;
				inviteLinkEmail = r.email;
			}
			inviteEmail = '';
		});
	}
	const cancelInvite = (id: number) =>
		act.run(() => api(`/system/users/invite/${id}/delete`, { method: 'POST' }));

	// 초대 링크 재생성 — 새 토큰(이전 링크 무효)·TTL 리셋. 메일이 켜져 있으면 재발송,
	// 아니면 새 링크를 (신규 초대와 같은 자리에) 노출한다. 만료된 초대도 가능.
	function regenerate(id: number) {
		return act.run(async () => {
			inviteLink = '';
			inviteLinkEmail = '';
			const r = await api<{ email: string; link: string; mailed: boolean }>(
				`/system/users/invite/${id}/regenerate`,
				{ method: 'POST' }
			);
			act.notice = r.mailed
				? t('초대 메일을 다시 보냈습니다.')
				: t('초대 링크를 직접 전달하세요.');
			if (!r.mailed) {
				inviteLink = r.link;
				inviteLinkEmail = r.email;
			}
		});
	}
</script>

<h2>{t('사용자')}</h2>

<AlertBox error={act.error} notice={act.notice} />

<Toolbar>
	<span class="spacer"></span>
	<span class="muted">{t('총')} {d.total}{t('건')}</span>
	<PageSize value={d.limit} options={d.limits} onchange={(n) => list.go({ limit: n, page: 1 })} />
</Toolbar>

<div class="table-wrap wide cards">
	<table>
		<thead>
			<tr><th>{t('이메일')}</th><th>{t('표시이름')}</th><th>{t('역할')}</th><th></th></tr>
		</thead>
		<tbody>
			{#each d.users as u}
				<tr>
					<td data-label={t('이메일')}>
						{u.email}
						{#if u.is_founder}<Badge variant="same">{t('최초 관리자')}</Badge>{/if}
					</td>
					<td data-label={t('표시이름')}>
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
					<td data-label={t('역할')}>
						{#if u.is_founder || u.id === d.me_id}
							<!-- 최초 관리자·본인 행은 역할 편집 불가(읽기 전용) — 셀렉트 오조작으로
							     자기 역할을 강등해 관리 접근을 잃는 실수를 막는다(서버 last-admin 락과 이중). -->
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
{#if inviteLink}
	<div class="notice link-out">
		{#if inviteLinkEmail}<strong>{inviteLinkEmail}</strong> · {/if}<span class="mono">{inviteLink}</span>
	</div>
{/if}

{#if d.invites.length > 0}
	<div class="table-wrap cards">
		<table>
			<thead><tr><th>{t('이메일')}</th><th>{t('역할')}</th><th>{t('만료')}</th><th></th></tr></thead>
			<tbody>
				{#each d.invites as inv}
					<tr>
						<td data-label={t('이메일')}>{inv.email}</td>
						<td data-label={t('역할')}>{d.role_labels[inv.role] ?? inv.role}</td>
						<td data-label={t('만료')}>
							{#if inv.expired}
								<Badge variant="error">{t('만료됨')}</Badge>
							{:else}
								<span class="mono muted">{ts(inv.expires_at)}</span>
							{/if}
						</td>
						<td>
							<div class="action-bar">
								<Button variant="outline" size="sm" onclick={() => regenerate(inv.id)} disabled={act.busy}>{t('재생성')}</Button>
								<Button variant="outline" size="sm" onclick={() => cancelInvite(inv.id)} disabled={act.busy}>{t('취소')}</Button>
							</div>
						</td>
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
