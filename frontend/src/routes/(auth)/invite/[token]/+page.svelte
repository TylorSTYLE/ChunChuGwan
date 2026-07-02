<script lang="ts">
	import { resolve } from '$app/paths';
	import { t } from '$lib/i18n';
	import { api, ApiError } from '$lib/api';
	import { afterAuth } from '$lib/auth';
	import type { LoginResult } from '$lib/types';
	import { Button } from '$lib/components/ui/button';
	import { Input } from '$lib/components/ui/input';

	let { data } = $props();

	let password = $state('');
	let error = $state('');
	let busy = $state(false);

	async function submit(e: SubmitEvent) {
		e.preventDefault();
		busy = true;
		error = '';
		try {
			const res = await api<LoginResult>(`/auth/invite/${encodeURIComponent(data.token)}`, {
				method: 'POST',
				body: JSON.stringify({ password }),
				redirectOn401: false
			});
			await afterAuth(res.status);
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
			busy = false;
		}
	}
</script>

<div class="auth-card">
	<h2>{t('초대 수락')}</h2>
	{#if data.problem}
		<div class="error">
			{t('유효하지 않거나 만료된 초대 링크입니다. 관리자에게 다시 초대를 요청하세요.')}
		</div>
		<div class="alt muted"><a href={resolve('/login')}>{t('로그인으로')}</a></div>
	{:else}
		{#if error}<div class="error">{error}</div>{/if}
		<form onsubmit={submit}>
			<label
				>{t('이메일')}
				<Input type="email" value={data.email} readonly autocomplete="username" />
			</label>
			<label
				>{t('패스워드')} <span class="muted">{t('(8자 이상)')}</span>
				<Input
					type="password"
					bind:value={password}
					minlength={8}
					required
					autocomplete="new-password"
				/>
			</label>
			<Button type="submit" disabled={busy} class="mt-1 w-full">{t('초대 수락')}</Button>
		</form>
	{/if}
</div>
