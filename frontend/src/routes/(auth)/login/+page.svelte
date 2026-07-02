<script lang="ts">
	import { resolve } from '$app/paths';
	import type { ResolvedPathname } from '$app/types';
	import { t } from '$lib/i18n';
	import { api, ApiError } from '$lib/api';
	import { afterAuth } from '$lib/auth';
	import type { AuthConfig, LoginResult } from '$lib/types';
	import { Button } from '$lib/components/ui/button';
	import { Input } from '$lib/components/ui/input';

	let { data }: { data: { config: AuthConfig } } = $props();
	const cfg = $derived(data.config);

	let email = $state('');
	let password = $state('');
	let error = $state('');
	let busy = $state(false);

	// OIDC 콜백이 SPA 현황(루트)으로 돌아오도록 next 를 base(=/) 로 넘긴다(백엔드 safe_next 허용).
	// /auth/oidc/login 자체는 SvelteKit 라우트가 아닌 백엔드 리다이렉트 엔드포인트라
	// resolve() 대상이 될 수 없어 최종 문자열만 ResolvedPathname 으로 좁힌다.
	const oidcHref = $derived(
		`/auth/oidc/login?next=${encodeURIComponent(resolve('/'))}` as ResolvedPathname
	);

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
			<Input type="email" bind:value={email} required autocomplete="username" />
		</label>
		<label
			>{t('패스워드')}
			<Input type="password" bind:value={password} required autocomplete="current-password" />
		</label>
		<Button type="submit" disabled={busy} class="mt-1 w-full">{t('로그인')}</Button>
	</form>
	{#if cfg.oidc_enabled}
		<div class="alt"><a href={oidcHref}>{t('SSO 로그인 →')}</a></div>
	{/if}
	{#if cfg.signup_enabled}
		<div class="alt muted">
			{t('계정이 없나요?')} <a href={resolve('/signup')}>{t('가입하기')}</a>
		</div>
	{/if}
</div>
