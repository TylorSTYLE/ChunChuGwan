<script lang="ts">
	import { invalidateAll } from '$app/navigation';
	import { t } from '$lib/i18n';
	import { filesize } from '$lib/format';
	import { api, ApiError } from '$lib/api';
	import type { SystemOverview } from '$lib/types';

	let { data }: { data: { sys: SystemOverview } } = $props();
	const s = $derived(data.sys);

	let error = $state('');
	let notice = $state('');
	let busy = $state(false);

	// 설정 폼 로컬 상태 — load 결과로 초기화/동기화
	let signupEnabled = $state(false);
	let signupRole = $state('pending');
	let evEnabled = $state(false);
	let evTtl = $state(30);
	let crawlMaxPages = $state(0);
	let crawlMaxDepth = $state(0);
	let crawlDelay = $state(0);
	let crawlBackoff = $state('');
	let credTtl = $state(0);
	let mobileShot = $state(false);
	let docCount = $state(0);
	let docMb = $state(0);
	let docTimeout = $state(0);

	// 네트워크 태그 폼
	let newTagName = $state('');
	let newTagDesc = $state('');
	let mergeSource = $state('');
	let mergeTarget = $state('');

	// SMTP 폼
	let smtpHost = $state('');
	let smtpPort = $state(587);
	let smtpUser = $state('');
	let smtpFrom = $state('');
	let smtpTls = $state('starttls');
	let smtpPassword = $state('');
	let smtpClearPw = $state(false);

	$effect(() => {
		signupEnabled = s.signup_enabled;
		signupRole = s.signup_default_role;
		evEnabled = s.email_verification_enabled;
		evTtl = s.email_verification_ttl_minutes;
		crawlMaxPages = s.crawl_defaults.max_pages;
		crawlMaxDepth = s.crawl_defaults.max_depth;
		crawlDelay = s.crawl_defaults.delay;
		crawlBackoff = s.crawl_retry_backoff;
		credTtl = s.ext_credential_ttl_hours;
		mobileShot = s.mobile_screenshot_enabled;
		docCount = s.document_limits.max_count;
		docMb = s.document_limits.max_mb;
		docTimeout = s.document_limits.timeout_seconds;
		smtpHost = s.smtp_config.host;
		smtpPort = s.smtp_config.port;
		smtpUser = s.smtp_config.user;
		smtpFrom = s.smtp_config.sender;
		smtpTls = s.smtp_config.tls;
	});

	async function save(path: string, body?: Record<string, unknown>): Promise<boolean> {
		busy = true;
		error = '';
		notice = '';
		try {
			await api(path, { method: 'POST', ...(body ? { body: JSON.stringify(body) } : {}) });
			notice = t('저장했습니다.');
			await invalidateAll();
			return true;
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
			return false;
		} finally {
			busy = false;
		}
	}

	async function createTag() {
		if (await save('/system/network-tags', { name: newTagName, description: newTagDesc })) {
			newTagName = '';
			newTagDesc = '';
		}
	}

	async function mergeTags() {
		if (await save('/system/network-tags/merge', { source: mergeSource, target: mergeTarget })) {
			mergeSource = '';
			mergeTarget = '';
		}
	}

	async function saveSmtp() {
		const ok = await save('/system/smtp-settings', {
			smtp_host: smtpHost,
			smtp_port: smtpPort,
			smtp_user: smtpUser,
			smtp_from: smtpFrom,
			smtp_tls: smtpTls,
			smtp_password: smtpPassword,
			smtp_clear_password: smtpClearPw
		});
		if (ok) {
			smtpPassword = '';
			smtpClearPw = false;
		}
	}

	async function testSmtp() {
		busy = true;
		error = '';
		notice = '';
		try {
			await api('/system/smtp-test', { method: 'POST' });
			notice = t('테스트 메일을 보냈습니다.');
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
		} finally {
			busy = false;
		}
	}
</script>

<h2>{t('시스템')}</h2>
{#if error}<div class="error">{error}</div>{/if}
{#if notice}<div class="notice">{notice}</div>{/if}

<div class="stat-grid">
	<div class="stat-card"><div class="label">{t('버전')}</div><div class="value">{s.version}</div></div>
	<div class="stat-card"><div class="label">{t('페이지')}</div><div class="value">{s.counts.pages}</div></div>
	<div class="stat-card"><div class="label">{t('스냅샷')}</div><div class="value">{s.counts.snapshots}</div></div>
	<div class="stat-card"><div class="label">{t('사용자')}</div><div class="value">{s.counts.users}</div></div>
</div>

<h3>{t('저장 용량')}</h3>
<table style="max-width:480px">
	<tbody>
		<tr><th>DB</th><td class="num mono">{filesize(s.usage.db)}</td></tr>
		<tr><th>{t('사이트')}</th><td class="num mono">{filesize(s.usage.sites)}</td></tr>
		<tr><th>{t('공유 자원')}</th><td class="num mono">{filesize(s.usage.resources)}</td></tr>
		<tr><th>{t('문서')}</th><td class="num mono">{filesize(s.usage.documents)}</td></tr>
	</tbody>
</table>

<fieldset class="sec">
	<legend>{t('가입 설정')}</legend>
	<label class="ck"><input type="checkbox" bind:checked={signupEnabled} /> {t('회원 가입 허용')}</label>
	<label>{t('가입 초기 권한')}
		<select bind:value={signupRole}>
			{#each s.signup_roles as r}<option value={r}>{s.role_labels[r] ?? r}</option>{/each}
		</select>
	</label>
	<button disabled={busy} onclick={() => save('/system/settings', { signup_enabled: signupEnabled, signup_default_role: signupRole })}>{t('저장')}</button>
</fieldset>

<fieldset class="sec">
	<legend>{t('이메일 본인 인증')}</legend>
	<label class="ck"><input type="checkbox" bind:checked={evEnabled} /> {t('사용')}</label>
	<label>{t('코드 만료(분)')} <input type="number" bind:value={evTtl} min={s.email_verification_ttl_limits.min} max={s.email_verification_ttl_limits.max} /></label>
	<button disabled={busy} onclick={() => save('/system/email-verification-settings', { email_verification_enabled: evEnabled, email_verification_ttl_minutes: evTtl })}>{t('저장')}</button>
</fieldset>

<fieldset class="sec">
	<legend>{t('사이트 아카이브 기본값')}</legend>
	<label>{t('최대 페이지')} <input type="number" bind:value={crawlMaxPages} /></label>
	<label>{t('최대 깊이')} <input type="number" bind:value={crawlMaxDepth} /></label>
	<label>{t('지연(초)')} <input type="number" bind:value={crawlDelay} /></label>
	<label>{t('재시도 대기(초, 쉼표)')} <input type="text" bind:value={crawlBackoff} /></label>
	<button disabled={busy} onclick={() => save('/system/crawl-settings', { crawl_max_pages: crawlMaxPages, crawl_max_depth: crawlMaxDepth, crawl_delay: crawlDelay, crawl_retry_backoff: crawlBackoff })}>{t('저장')}</button>
</fieldset>

<fieldset class="sec">
	<legend>{t('확장 자격증명')}</legend>
	<label>{t('보관 시간(시간)')} <input type="number" bind:value={credTtl} min={s.ext_credential_ttl_limits.min} max={s.ext_credential_ttl_limits.max} /></label>
	<button disabled={busy} onclick={() => save('/system/credential-settings', { ext_credential_ttl_hours: credTtl })}>{t('저장')}</button>
</fieldset>

<fieldset class="sec">
	<legend>{t('캡처')}</legend>
	<label class="ck"><input type="checkbox" bind:checked={mobileShot} /> {t('모바일 스크린샷도 저장')}</label>
	<button disabled={busy} onclick={() => save('/system/capture-settings', { mobile_screenshot_enabled: mobileShot })}>{t('저장')}</button>
</fieldset>

<fieldset class="sec">
	<legend>{t('문서 아카이브 한도')}</legend>
	<label>{t('스냅샷당 수')} <input type="number" bind:value={docCount} /></label>
	<label>{t('개당 크기(MB)')} <input type="number" bind:value={docMb} /></label>
	<label>{t('다운로드 타임아웃(초)')} <input type="number" bind:value={docTimeout} /></label>
	<button disabled={busy} onclick={() => save('/system/document-settings', { document_max_count: docCount, document_max_mb: docMb, document_fetch_timeout: docTimeout })}>{t('저장')}</button>
</fieldset>

<h3>{t('로컬 네트워크 태그')}</h3>
<fieldset class="sec">
	<legend>{t('태그 추가')}</legend>
	<label>{t('이름')} <input type="text" bind:value={newTagName} maxlength="60" /></label>
	<label>{t('설명')} <input type="text" bind:value={newTagDesc} maxlength="200" /></label>
	<button disabled={busy || !newTagName.trim()} onclick={createTag}>{t('추가')}</button>
</fieldset>
{#if s.network_tags.length === 0}
	<p class="muted">{t('등록된 태그가 없습니다.')}</p>
{:else}
	<ul class="taglist">
		{#each s.network_tags as tag}
			<li>
				<span class="mono">{tag.name}</span>
				<span class="muted mono">{tag.id}</span>
				<button class="del" disabled={busy} onclick={() => save(`/system/network-tags/${tag.id}/delete`)}>{t('삭제')}</button>
			</li>
		{/each}
	</ul>
	{#if s.network_tags.length >= 2}
		<fieldset class="sec">
			<legend>{t('태그 병합')}</legend>
			<label>{t('원본')}
				<select bind:value={mergeSource}>
					<option value="">—</option>
					{#each s.network_tags as tag}<option value={tag.id}>{tag.name}</option>{/each}
				</select>
			</label>
			<label>{t('대상')}
				<select bind:value={mergeTarget}>
					<option value="">—</option>
					{#each s.network_tags as tag}<option value={tag.id}>{tag.name}</option>{/each}
				</select>
			</label>
			<button disabled={busy || !mergeSource || !mergeTarget} onclick={mergeTags}>{t('병합')}</button>
		</fieldset>
	{/if}
{/if}

<h3>{t('메일(SMTP)')}</h3>
<fieldset class="sec">
	<legend>{s.smtp_config.enabled ? t('사용 중') : t('미설정')}</legend>
	<label>{t('호스트')} <input type="text" bind:value={smtpHost} /></label>
	<label>{t('포트')} <input type="number" bind:value={smtpPort} min="1" max="65535" /></label>
	<label>{t('사용자')} <input type="text" bind:value={smtpUser} /></label>
	<label>{t('보내는 주소')} <input type="text" bind:value={smtpFrom} /></label>
	<label>TLS
		<select bind:value={smtpTls}>
			{#each s.smtp_tls_modes as m}<option value={m}>{m}</option>{/each}
		</select>
	</label>
	<label>{t('비밀번호')}
		<input type="password" bind:value={smtpPassword}
			placeholder={s.smtp_config.has_password ? '••••••••' : ''} />
	</label>
	{#if s.smtp_config.has_password}
		<label class="ck"><input type="checkbox" bind:checked={smtpClearPw} /> {t('저장된 비밀번호 삭제')}</label>
	{/if}
	<div class="btn-row">
		<button disabled={busy} onclick={saveSmtp}>{t('저장')}</button>
		<button disabled={busy || !s.smtp_config.enabled} onclick={testSmtp}>{t('테스트 메일 보내기')}</button>
	</div>
</fieldset>

<p class="muted" style="font-size:12px; margin-top:20px">
	{t('백업·복원·데이터 이전·재색인은 이어서 추가됩니다.')}
</p>

<style>
	.error {
		background: var(--red-bg);
		color: var(--red-text);
		border-radius: 4px;
		padding: 8px 12px;
		margin-bottom: 12px;
		font-size: 13px;
	}
	.sec {
		border: 1px solid var(--border);
		border-radius: 6px;
		margin: 14px 0;
		padding: 10px 14px;
		max-width: 560px;
		display: flex;
		flex-direction: column;
		gap: 8px;
	}
	.sec legend {
		font-size: 13px;
		font-weight: 600;
		padding: 0 4px;
	}
	.sec label {
		font-size: 13px;
		display: flex;
		justify-content: space-between;
		align-items: center;
		gap: 8px;
	}
	.sec label.ck {
		justify-content: flex-start;
	}
	.sec button {
		align-self: flex-start;
	}
	.taglist {
		list-style: none;
		padding: 0;
		max-width: 560px;
	}
	.taglist li {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 4px 0;
		font-size: 13px;
	}
	.taglist li .del {
		margin-left: auto;
		font-size: 12px;
	}
	.btn-row {
		display: flex;
		gap: 8px;
	}
</style>
