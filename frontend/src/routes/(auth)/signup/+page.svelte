<script lang="ts">
	import { base } from '$app/paths';
	import { t } from '$lib/i18n';
	import { api, ApiError } from '$lib/api';
	import { afterAuth } from '$lib/auth';
	import type { LoginResult } from '$lib/types';

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
	{#if error}<div class="error">{error}</div>{/if}
	<form onsubmit={submit}>
		<label
			>{t('이메일')}
			<input type="email" bind:value={email} required autocomplete="username" />
		</label>
		<label
			>{t('패스워드')} <span class="muted">{t('(8자 이상)')}</span>
			<input
				type="password"
				bind:value={password}
				minlength="8"
				required
				autocomplete="new-password"
			/>
		</label>
		<button type="submit" disabled={busy}>{t('가입')}</button>
	</form>
	<div class="alt muted">
		{t('이미 계정이 있나요?')} <a href="{base}/login">{t('로그인')}</a>
	</div>
</div>
