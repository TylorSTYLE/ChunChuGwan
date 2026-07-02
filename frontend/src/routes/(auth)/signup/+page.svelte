<script lang="ts">
	import { base } from '$app/paths';
	import { t } from '$lib/i18n';
	import { api, ApiError } from '$lib/api';
	import { afterAuth } from '$lib/auth';
	import type { LoginResult, AuthConfig } from '$lib/types';
	import { Button } from '$lib/components/ui/button';
	import { Input } from '$lib/components/ui/input';

	let { data }: { data: { config: AuthConfig } } = $props();

	let email = $state('');
	let password = $state('');
	let error = $state('');
	let busy = $state(false);

	async function submit(e: SubmitEvent) {
		e.preventDefault();
		busy = true;
		error = '';
		try {
			const res = await api<LoginResult>('/auth/signup', {
				method: 'POST',
				body: JSON.stringify({ email: email.trim(), password }),
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
	<h2>{t('가입')}</h2>
	{#if !data.config.signup_enabled}
		<!-- 가입 비활성 상태에서 URL 직접 진입 시 폼을 렌더하지 않는다(제출 시점 403 대신 즉시 안내). -->
		<p class="muted">{t('회원 가입이 비활성화되어 있습니다.')}</p>
		<div class="alt muted">
			{t('이미 계정이 있나요?')} <a href="{base}/login">{t('로그인')}</a>
		</div>
	{:else}
		{#if error}<div class="error">{error}</div>{/if}
		<form onsubmit={submit}>
			<label
				>{t('이메일')}
				<Input type="email" bind:value={email} required autocomplete="username" />
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
			<Button type="submit" disabled={busy} class="mt-1 w-full">{t('가입')}</Button>
		</form>
		<div class="alt muted">
			{t('이미 계정이 있나요?')} <a href="{base}/login">{t('로그인')}</a>
		</div>
	{/if}
</div>
