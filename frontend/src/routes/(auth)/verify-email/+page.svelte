<script lang="ts">
	import { invalidateAll } from '$app/navigation';
	import { t } from '$lib/i18n';
	import { api, ApiError } from '$lib/api';
	import { afterAuth } from '$lib/auth';
	import type { LoginResult, VerifyEmailStatus } from '$lib/types';

	let { data }: { data: { status: VerifyEmailStatus } } = $props();
	const st = $derived(data.status);

	let code = $state('');
	let error = $state('');
	let notice = $state('');
	let busy = $state(false);

	async function submit(e: SubmitEvent) {
		e.preventDefault();
		busy = true;
		error = '';
		notice = '';
		try {
			const res = await api<LoginResult>('/auth/verify-email', {
				method: 'POST',
				body: JSON.stringify({ code: code.trim() }),
				redirectOn401: false
			});
			await afterAuth(res.status);
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
			busy = false;
		}
	}

	async function resend() {
		busy = true;
		error = '';
		notice = '';
		try {
			await api('/auth/verify-email/resend', { method: 'POST', redirectOn401: false });
			notice = t('인증 코드를 다시 보냈습니다.');
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
		} finally {
			busy = false;
			await invalidateAll();
		}
	}
</script>

<div class="auth-card">
	<h2>{t('이메일 본인 인증')}</h2>
	{#if notice}<div class="notice">{notice}</div>{/if}
	{#if error}<div class="error">{error}</div>{/if}

	{#if st.mail_enabled}
		<p class="muted">
			<span class="mono">{st.email}</span>
			{t('(으)로 보낸 인증 코드를 입력하세요.')}
			{t('코드는')}
			{st.ttl_minutes}{t('분 후 만료됩니다.')}
		</p>
		<form onsubmit={submit}>
			<label
				>{t('인증 코드')}
				<input
					type="text"
					inputmode="numeric"
					pattern="[0-9]*"
					maxlength="6"
					autocomplete="one-time-code"
					class="mono"
					bind:value={code}
					required
				/>
			</label>
			<button type="submit" disabled={busy || !code.trim()}>{t('확인')}</button>
		</form>

		<div class="alt">
			<p class="muted">{t('코드를 받지 못했나요?')}</p>
			<button type="button" onclick={resend} disabled={busy}>{t('인증 코드 다시 보내기')}</button>
		</div>
	{:else}
		<div class="error">
			{t('메일 발송(SMTP)이 설정되지 않아 인증 코드를 보낼 수 없습니다. 관리자에게 문의하세요.')}
		</div>
	{/if}

	{#if st.pending}
		<div class="alt">
			<form method="POST" action="/logout"><button type="submit">{t('로그아웃')}</button></form>
		</div>
	{/if}
</div>
