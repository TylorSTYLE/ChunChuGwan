<script lang="ts">
	import { untrack } from 'svelte';
	import { invalidateAll } from '$app/navigation';
	import { t } from '$lib/i18n';
	import { ts } from '$lib/format';
	import { api, ApiError } from '$lib/api';
	import { b64uToBuf, bufToB64u } from '$lib/webauthn';
	import type { AccountData } from '$lib/types';

	let { data }: { data: { data: AccountData } } = $props();
	const d = $derived(data.data);

	let error = $state('');
	let notice = $state('');
	let busy = $state(false);

	// 초기값은 로드 시점 1회만 캡처 — 입력 중 invalidateAll 로 덮어쓰지 않는다.
	let displayName = $state(untrack(() => data.data.display_name));
	let locale = $state(untrack(() => data.data.locale));
	let timezone = $state(untrack(() => data.data.timezone));

	let curPw = $state('');
	let newPw = $state('');
	let newPw2 = $state('');

	let withdrawConfirm = $state('');
	let withdrawPw = $state('');

	async function run(fn: () => Promise<void>, ok: string) {
		busy = true;
		error = '';
		notice = '';
		try {
			await fn();
			notice = t(ok);
			await invalidateAll();
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
		} finally {
			busy = false;
		}
	}

	const saveName = () =>
		run(async () => {
			await api('/settings/account/name', {
				method: 'POST',
				body: JSON.stringify({ display_name: displayName.trim() })
			});
		}, '표시 이름을 변경했습니다.');

	const saveLanguage = () =>
		run(async () => {
			await api('/settings/account/language', {
				method: 'POST',
				body: JSON.stringify({ locale })
			});
			// 언어 변경은 전역 i18n 에 영향 — 새로고침으로 반영
			if (typeof window !== 'undefined') window.location.reload();
		}, '언어를 변경했습니다.');

	const saveTimezone = () =>
		run(async () => {
			await api('/settings/account/timezone', {
				method: 'POST',
				body: JSON.stringify({ timezone })
			});
		}, '시간대를 변경했습니다.');

	const changePassword = () =>
		run(async () => {
			await api('/settings/account/password', {
				method: 'POST',
				body: JSON.stringify({
					current_password: curPw,
					new_password: newPw,
					new_password2: newPw2
				})
			});
			curPw = newPw = newPw2 = '';
		}, '패스워드를 변경했습니다. 다른 기기의 세션은 로그아웃되었습니다.');

	// ── 2단계 인증 (TOTP) ──
	let totpSetup = $state<{ secret: string; qr: string } | null>(null);
	let totpCode = $state('');
	let totpPassword = $state('');

	const startTotp = () =>
		run(async () => {
			totpSetup = await api<{ secret: string; qr: string }>('/settings/totp/setup', {
				method: 'POST'
			});
		}, '인증 앱에 등록한 뒤 코드를 입력하세요.');

	const confirmTotp = () =>
		run(async () => {
			await api('/settings/totp/confirm', {
				method: 'POST',
				body: JSON.stringify({ code: totpCode.trim() })
			});
			totpSetup = null;
			totpCode = '';
		}, '2단계 인증을 켰습니다.');

	const disableTotp = () =>
		run(async () => {
			await api('/settings/totp/disable', {
				method: 'POST',
				body: JSON.stringify({ password: totpPassword })
			});
			totpPassword = '';
		}, '2단계 인증을 껐습니다.');

	// ── 패스키 (WebAuthn) ──
	let pkName = $state('');
	let pkPasswords = $state<Record<number, string>>({});

	async function registerPasskey() {
		busy = true;
		error = '';
		notice = '';
		try {
			/* eslint-disable @typescript-eslint/no-explicit-any */
			const opts = await api<any>('/settings/passkey/options', { method: 'POST' });
			opts.challenge = b64uToBuf(opts.challenge);
			opts.user.id = b64uToBuf(opts.user.id);
			(opts.excludeCredentials || []).forEach((c: any) => (c.id = b64uToBuf(c.id)));
			const cred = (await navigator.credentials.create({
				publicKey: opts
			})) as PublicKeyCredential;
			const att = cred.response as AuthenticatorAttestationResponse;
			await api('/settings/passkey/register', {
				method: 'POST',
				body: JSON.stringify({
					name: pkName,
					credential: {
						id: cred.id,
						rawId: bufToB64u(cred.rawId),
						type: cred.type,
						clientExtensionResults: cred.getClientExtensionResults(),
						response: {
							clientDataJSON: bufToB64u(att.clientDataJSON),
							attestationObject: bufToB64u(att.attestationObject),
							transports: att.getTransports ? att.getTransports() : []
						}
					}
				})
			});
			pkName = '';
			notice = t('패스키를 등록했습니다.');
			await invalidateAll();
		} catch (err) {
			error =
				err instanceof ApiError
					? err.message
					: (err as Error)?.message || t('패스키 등록이 취소되었습니다.');
		} finally {
			busy = false;
		}
	}

	function deletePasskey(id: number) {
		const pw = pkPasswords[id] || '';
		if (!pw) return;
		return run(async () => {
			await api(`/settings/passkey/${id}/delete`, {
				method: 'POST',
				body: JSON.stringify({ password: pw })
			});
			delete pkPasswords[id];
		}, '패스키를 삭제했습니다.');
	}

	async function withdraw() {
		if (!confirm(t('정말 탈퇴할까요? 이 작업은 되돌릴 수 없습니다.'))) return;
		busy = true;
		error = '';
		try {
			await api('/settings/account/withdraw', {
				method: 'POST',
				body: JSON.stringify(
					d.has_password ? { password: withdrawPw } : { confirm: withdrawConfirm }
				)
			});
			if (typeof window !== 'undefined') window.location.href = '/login';
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
			busy = false;
		}
	}
</script>

<h2>{t('계정')}</h2>
{#if error}<div class="error">{error}</div>{/if}
{#if notice}<div class="notice">{notice}</div>{/if}

<dl class="meta">
	<dt>{t('이메일')}</dt>
	<dd class="mono">{d.email}</dd>
	<dt>{t('역할')}</dt>
	<dd>{d.role_label}</dd>
	<dt>{t('2단계 인증(TOTP)')}</dt>
	<dd>{d.totp_enabled ? t('사용 중') : t('미설정')}</dd>
	<dt>{t('패스키')}</dt>
	<dd>{d.passkey_count}{t('개')}</dd>
	{#if d.email_verification_on}
		<dt>{t('이메일 인증')}</dt>
		<dd>{d.email_verified ? t('인증됨') : t('미인증')}</dd>
	{/if}
</dl>

<section>
	<h3>{t('표시 이름')}</h3>
	<div class="form">
		<input type="text" bind:value={displayName} placeholder={d.email} />
		<button onclick={saveName} disabled={busy}>{t('저장')}</button>
	</div>
	<p class="muted hint">{t('비우면 이메일이 표시됩니다.')}</p>
</section>

<section>
	<h3>{t('언어')}</h3>
	<div class="form">
		<select bind:value={locale}>
			{#each d.locales as code}
				<option value={code}>{d.locale_names[code] ?? code}</option>
			{/each}
		</select>
		<button onclick={saveLanguage} disabled={busy}>{t('저장')}</button>
	</div>
</section>

<section>
	<h3>{t('시간대')}</h3>
	<div class="form">
		<select bind:value={timezone}>
			{#each d.timezones as tz}<option value={tz}>{tz}</option>{/each}
		</select>
		<button onclick={saveTimezone} disabled={busy}>{t('저장')}</button>
	</div>
</section>

{#if d.has_password}
	<section>
		<h3>{t('패스워드 변경')}</h3>
		<div class="form col">
			<input type="password" bind:value={curPw} placeholder={t('현재 패스워드')} autocomplete="current-password" />
			<input type="password" bind:value={newPw} placeholder={t('새 패스워드')} autocomplete="new-password" />
			<input type="password" bind:value={newPw2} placeholder={t('새 패스워드 확인')} autocomplete="new-password" />
			<button onclick={changePassword} disabled={busy || !curPw || !newPw}>{t('변경')}</button>
		</div>
	</section>
{:else}
	<section>
		<h3>{t('패스워드 변경')}</h3>
		<p class="muted">{t('SSO 전용 계정은 패스워드가 없습니다. IdP(Authentik)에서 관리하세요.')}</p>
	</section>
{/if}

<section>
	<h3>{t('2단계 인증 (TOTP)')}</h3>
	{#if !d.has_password}
		<p class="muted">{t('SSO 전용 계정의 2단계 인증은 IdP(Authentik)에서 관리합니다.')}</p>
	{:else if d.totp_enabled}
		<p class="muted">{t('사용 중입니다.')}</p>
		<div class="form">
			<input
				type="password"
				bind:value={totpPassword}
				placeholder={t('현재 패스워드')}
				autocomplete="current-password"
			/>
			<button class="danger" onclick={disableTotp} disabled={busy || !totpPassword}>{t('해제')}</button>
		</div>
	{:else if totpSetup}
		<p class="muted hint">
			{t('인증 앱(Google Authenticator 등)으로 QR 을 스캔하거나 키를 입력한 뒤, 표시되는 코드를 입력하세요.')}
		</p>
		<img class="qr" src={totpSetup.qr} alt={t('TOTP QR')} />
		<div class="mono secret">{totpSetup.secret}</div>
		<div class="form">
			<input type="text" inputmode="numeric" bind:value={totpCode} placeholder={t('6자리 코드')} />
			<button onclick={confirmTotp} disabled={busy || !totpCode.trim()}>{t('확인')}</button>
			<button onclick={() => (totpSetup = null)} disabled={busy}>{t('취소')}</button>
		</div>
	{:else}
		<button onclick={startTotp} disabled={busy}>{t('2단계 인증 설정')}</button>
	{/if}
</section>

<section>
	<h3>{t('패스키')}</h3>
	{#if !d.has_password}
		<p class="muted">{t('SSO 전용 계정의 2단계 인증은 IdP(Authentik)에서 관리합니다.')}</p>
	{:else}
		<p class="muted hint">
			{t('Touch ID·보안 키·휴대폰 등을 패스워드 로그인의 2단계 인증 수단으로 등록합니다.')}
		</p>
		{#if d.passkeys.length > 0}
			<div class="table-wrap">
				<table>
					<thead>
						<tr>
							<th>{t('이름')}</th>
							<th>{t('등록')}</th>
							<th>{t('마지막 사용')}</th>
							<th></th>
						</tr>
					</thead>
					<tbody>
						{#each d.passkeys as pk}
							<tr>
								<td>{pk.name}</td>
								<td class="mono muted">{ts(pk.created_at)}</td>
								<td class="mono muted">{pk.last_used_at ? ts(pk.last_used_at) : '—'}</td>
								<td class="pk-del">
									<input
										type="password"
										bind:value={pkPasswords[pk.id]}
										placeholder={t('패스워드 확인')}
									/>
									<button
										class="danger"
										onclick={() => deletePasskey(pk.id)}
										disabled={busy || !pkPasswords[pk.id]}>{t('삭제')}</button
									>
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{:else}
			<p class="muted">{t('등록된 패스키가 없습니다.')}</p>
		{/if}
		<div class="form">
			<input
				type="text"
				bind:value={pkName}
				maxlength="64"
				placeholder={t('새 패스키 이름 (예: 맥북 Touch ID)')}
			/>
			<button onclick={registerPasskey} disabled={busy}>{t('패스키 등록')}</button>
		</div>
	{/if}
</section>

{#if !d.is_admin}
	<section class="danger-zone">
		<h3>{t('위험 영역')}</h3>
		<p class="muted">{t('탈퇴하면 모든 세션이 종료되고 같은 이메일로 재가입할 수 없습니다 (관리자 삭제 전까지).')}</p>
		<div class="form">
			{#if d.has_password}
				<input type="password" bind:value={withdrawPw} placeholder={t('현재 패스워드')} autocomplete="current-password" />
			{:else}
				<input type="text" bind:value={withdrawConfirm} placeholder={t('확인을 위해 이메일 입력')} />
			{/if}
			<button class="danger" onclick={withdraw} disabled={busy}>{t('탈퇴')}</button>
		</div>
	</section>
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
	dl.meta {
		display: grid;
		grid-template-columns: max-content 1fr;
		gap: 4px 16px;
		font-size: 13px;
		margin: 0 0 8px;
	}
	dl.meta dt {
		color: var(--muted);
	}
	dl.meta dd {
		margin: 0;
	}
	section {
		border-top: 1px solid var(--border);
		padding: 12px 0;
	}
	section h3 {
		font-size: 14px;
		margin: 0 0 8px;
	}
	.form {
		display: flex;
		gap: 8px;
		align-items: center;
		flex-wrap: wrap;
	}
	.form.col {
		flex-direction: column;
		align-items: stretch;
		max-width: 320px;
	}
	.hint {
		font-size: 12px;
		margin: 4px 0 0;
	}
	.qr {
		display: block;
		width: 180px;
		height: 180px;
		margin: 8px 0;
		image-rendering: pixelated;
		background: #fff;
		border: 1px solid var(--border);
		border-radius: 4px;
	}
	.secret {
		font-size: 13px;
		word-break: break-all;
		margin-bottom: 8px;
	}
	.pk-del {
		display: flex;
		gap: 6px;
		align-items: center;
	}
	.pk-del input {
		width: 130px;
	}
	.danger-zone {
		border-top-color: var(--red);
	}
	button.danger {
		color: #fff;
		background: var(--red);
		border-color: var(--red);
	}
</style>
