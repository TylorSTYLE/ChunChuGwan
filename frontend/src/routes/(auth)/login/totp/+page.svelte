<script lang="ts">
	import { t } from '$lib/i18n';
	import { api, ApiError } from '$lib/api';
	import { afterAuth } from '$lib/auth';
	import { b64uToBuf, bufToB64u } from '$lib/webauthn';
	import type { LoginResult, TotpStatus } from '$lib/types';

	let { data }: { data: { status: TotpStatus } } = $props();
	const st = $derived(data.status);

	let code = $state('');
	let error = $state('');
	let busy = $state(false);

	async function submitTotp(e: SubmitEvent) {
		e.preventDefault();
		busy = true;
		error = '';
		try {
			const res = await api<LoginResult>('/auth/login/totp', {
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

	async function loginPasskey() {
		busy = true;
		error = '';
		try {
			/* eslint-disable @typescript-eslint/no-explicit-any */
			const opts = await api<any>('/auth/login/passkey/options', {
				method: 'POST',
				redirectOn401: false
			});
			opts.challenge = b64uToBuf(opts.challenge);
			(opts.allowCredentials || []).forEach((c: any) => (c.id = b64uToBuf(c.id)));
			const cred = (await navigator.credentials.get({ publicKey: opts })) as PublicKeyCredential;
			const asr = cred.response as AuthenticatorAssertionResponse;
			const res = await api<LoginResult>('/auth/login/passkey', {
				method: 'POST',
				redirectOn401: false,
				body: JSON.stringify({
					credential: {
						id: cred.id,
						rawId: bufToB64u(cred.rawId),
						type: cred.type,
						clientExtensionResults: cred.getClientExtensionResults(),
						response: {
							clientDataJSON: bufToB64u(asr.clientDataJSON),
							authenticatorData: bufToB64u(asr.authenticatorData),
							signature: bufToB64u(asr.signature),
							userHandle: asr.userHandle ? bufToB64u(asr.userHandle) : null
						}
					}
				})
			});
			await afterAuth(res.status);
		} catch (err) {
			error =
				err instanceof ApiError
					? err.message
					: (err as Error)?.message || t('패스키 인증이 취소되었습니다.');
			busy = false;
		}
	}
</script>

<div class="auth-card">
	<h2>{t('2단계 인증')}</h2>
	{#if error}<div class="error">{error}</div>{/if}

	{#if st.has_passkey}
		<p class="muted">{t('등록된 패스키로 본인 확인을 완료하세요.')}</p>
		<button type="button" class="pk-btn" onclick={loginPasskey} disabled={busy}
			>{t('패스키로 인증')}</button
		>
	{/if}

	{#if st.has_totp}
		<div class:alt={st.has_passkey}>
			<p class="muted">{t('인증 앱에 표시된 6자리 코드를 입력하세요.')}</p>
			<form onsubmit={submitTotp}>
				<label
					>{t('OTP 코드')}
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
		</div>
	{/if}
</div>

<style>
	.pk-btn {
		width: 100%;
		padding: 6px;
		font: inherit;
		font-size: 13px;
		border: 1px solid var(--border);
		border-radius: 4px;
		background: var(--surface);
		color: var(--fg);
		cursor: pointer;
	}
	.pk-btn:hover {
		background: var(--bg-soft);
	}
</style>
