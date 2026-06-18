<script lang="ts">
	import { base } from '$app/paths';
	import { t } from '$lib/i18n';
	import { api, ApiError } from '$lib/api';
	import { afterAuth } from '$lib/auth';
	import type { AuthConfig, LoginResult } from '$lib/types';

	let { data }: { data: { config: AuthConfig } } = $props();
	const cfg = $derived(data.config);

	let email = $state('');
	let password = $state('');
	let error = $state('');
	let busy = $state(false);

	// OIDC 콜백이 SPA 현황으로 돌아오도록 next 를 /ui/ 로 넘긴다(백엔드 safe_next 허용).
	const oidcHref = $derived(`/auth/oidc/login?next=${encodeURIComponent(`${base}/`)}`);

	async function submit(e: SubmitEvent) {
		e.preventDefault();
		busy = true;
		error = '';
		try {
			const res = await api<LoginResult>('/auth/login', {
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
	<h2>{t('로그인')}</h2>
	{#if error}<div class="error">{error}</div>{/if}
	<form onsubmit={submit}>
		<label
			>{t('이메일')}
			<input type="email" bind:value={email} required autocomplete="username" />
		</label>
		<label
			>{t('패스워드')}
			<input type="password" bind:value={password} required autocomplete="current-password" />
		</label>
		<button type="submit" disabled={busy}>{t('로그인')}</button>
	</form>
	{#if cfg.oidc_enabled}
		<div class="alt"><a href={oidcHref}>{t('SSO 로그인 →')}</a></div>
	{/if}
	{#if cfg.signup_enabled}
		<div class="alt muted">
			{t('계정이 없나요?')} <a href="{base}/signup">{t('가입하기')}</a>
		</div>
	{/if}
</div>
