<script lang="ts">
	import { base } from '$app/paths';
	import { invalidateAll } from '$app/navigation';
	import { t } from '$lib/i18n';
	import { filesize } from '$lib/format';
	import { api, ApiError, download } from '$lib/api';
	import type { SystemOverview } from '$lib/types';
	import AlertBox from '$lib/components/AlertBox.svelte';
	import Spinner from '$lib/components/Spinner.svelte';

	let { data }: { data: { sys: SystemOverview } } = $props();
	const s = $derived(data.sys);

	// 저장 용량 미터 차트 — 각 영역이 전체에서 차지하는 비율
	const usageTotal = $derived(
		(s.usage.db + s.usage.sites + s.usage.resources + s.usage.documents) || 1
	);
	function pct(n: number): string {
		return `${Math.round((n / usageTotal) * 100)}%`;
	}

	let error = $state('');
	let notice = $state('');
	let busy = $state(false);
	// busy 는 모든 버튼을 동시에 잠그지만, 스피너는 지금 눌린 버튼에만 떠야 한다 —
	// 진행 중 작업 식별자(compact·/system/backup·/system/export)를 따로 둔다.
	let pending = $state('');

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

	// 인증 보호(rate limit)
	let atEnabled = $state(true);
	let atLoginLimit = $state(10);
	let atLoginIpLimit = $state(30);
	let atLoginWindow = $state(15);
	let atTotpLimit = $state(10);
	let atEmailVerifyLimit = $state(5);
	let atEmailResendLimit = $state(5);

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

	// 백업·복원·가져오기
	let importMode = $state('merge');

	// 유지보수 — 재색인 진행
	let reindexRunning = $state(false);
	let reindexDone = $state(0);
	let reindexTotal = $state(0);

	// 데이터 이전(마이그레이션) — 발급 토큰은 1회만 표시
	let migrationToken = $state('');

	$effect(() => {
		signupEnabled = s.signup_enabled;
		signupRole = s.signup_default_role;
		evEnabled = s.email_verification_enabled;
		evTtl = s.email_verification_ttl_minutes;
		atEnabled = s.auth_throttle_enabled;
		atLoginLimit = s.auth_throttle.login_limit;
		atLoginIpLimit = s.auth_throttle.login_ip_limit;
		atLoginWindow = s.auth_throttle.login_window_minutes;
		atTotpLimit = s.auth_throttle.totp_limit;
		atEmailVerifyLimit = s.auth_throttle.email_verify_limit;
		atEmailResendLimit = s.auth_throttle.email_resend_limit;
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

	async function doDownload(path: string) {
		busy = true;
		pending = path;
		error = '';
		notice = '';
		try {
			await download(path);
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
		} finally {
			busy = false;
			pending = '';
		}
	}

	async function migrationAction(action: 'enable' | 'regenerate' | 'disable') {
		busy = true;
		error = '';
		notice = '';
		try {
			const r = await api<{ token?: string }>(`/system/migration/${action}`, { method: 'POST' });
			migrationToken = r.token ?? '';
			notice = t('완료했습니다.');
			await invalidateAll();
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
		} finally {
			busy = false;
		}
	}

	async function compact() {
		busy = true;
		pending = 'compact';
		error = '';
		notice = '';
		try {
			const r = await api<{ ran: boolean }>('/system/compact', { method: 'POST' });
			notice = r.ran ? t('최적화했습니다.') : t('최적화할 항목이 없습니다.');
			await invalidateAll();
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
		} finally {
			busy = false;
			pending = '';
		}
	}

	async function pollReindex() {
		try {
			const s = await api<{ running: boolean; done: number; total: number; error: string | null }>(
				'/system/search/reindex/status'
			);
			reindexDone = s.done;
			reindexTotal = s.total;
			if (s.running) {
				setTimeout(pollReindex, 1000);
			} else {
				reindexRunning = false;
				notice = s.error ? `${t('재색인 실패')}: ${s.error}` : t('재색인을 완료했습니다.');
				await invalidateAll();
			}
		} catch (err) {
			reindexRunning = false;
			error = err instanceof ApiError ? err.message : String(err);
		}
	}

	async function startReindex() {
		error = '';
		notice = '';
		try {
			await api('/system/search/reindex', { method: 'POST' });
			reindexRunning = true;
			reindexDone = 0;
			reindexTotal = 0;
			pollReindex();
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
		}
	}

	async function uploadFile(e: Event, path: string, confirmMsg: string, extra: Record<string, string> = {}) {
		const input = e.currentTarget as HTMLInputElement;
		const file = input.files?.[0];
		if (!file) return;
		if (confirmMsg && !confirm(confirmMsg)) {
			input.value = '';
			return;
		}
		busy = true;
		error = '';
		notice = '';
		try {
			const fd = new FormData();
			fd.append('file', file);
			for (const [k, v] of Object.entries(extra)) fd.append(k, v);
			await api(path, { method: 'POST', body: fd });
			notice = t('완료했습니다.');
			await invalidateAll();
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
		} finally {
			busy = false;
			input.value = '';
		}
	}
</script>

<h2>{t('시스템 설정')}</h2>
<AlertBox {error} {notice} />

<!-- ── 시스템 상태 ── -->
<h3 class="group">{t('시스템 상태')}</h3>
<p class="desc">{t('현재 버전과 저장된 데이터 규모입니다.')}</p>
<div class="stat-grid">
	<div class="stat-card"><div class="label">{t('버전')}</div><div class="value">{s.version}</div></div>
	<div class="stat-card"><div class="label">{t('페이지')}</div><div class="value">{s.counts.pages}</div></div>
	<div class="stat-card"><div class="label">{t('스냅샷')}</div><div class="value">{s.counts.snapshots}</div></div>
	<div class="stat-card"><div class="label">{t('사용자')}</div><div class="value">{s.counts.users}</div></div>
</div>

<div class="meter-box">
	<div class="meter-head">
		<span>{t('저장 용량')}</span>
		<span class="mono muted">{filesize(usageTotal)}</span>
	</div>
	<div class="meter">
		<span class="seg seg-db" style="width:{pct(s.usage.db)}" title="DB {filesize(s.usage.db)}"></span>
		<span class="seg seg-sites" style="width:{pct(s.usage.sites)}" title="{t('사이트')} {filesize(s.usage.sites)}"></span>
		<span class="seg seg-res" style="width:{pct(s.usage.resources)}" title="{t('공유 자원')} {filesize(s.usage.resources)}"></span>
		<span class="seg seg-docs" style="width:{pct(s.usage.documents)}" title="{t('문서')} {filesize(s.usage.documents)}"></span>
	</div>
	<ul class="legend-list mono">
		<li><span class="dot seg-db"></span>DB {filesize(s.usage.db)}</li>
		<li><span class="dot seg-sites"></span>{t('사이트')} {filesize(s.usage.sites)}</li>
		<li><span class="dot seg-res"></span>{t('공유 자원')} {filesize(s.usage.resources)}</li>
		<li><span class="dot seg-docs"></span>{t('문서')} {filesize(s.usage.documents)}</li>
	</ul>
</div>

<!-- ── 유지관리 ── -->
<h3 class="group">{t('유지관리')}</h3>
<p class="desc">{t('검색 인덱스와 저장공간을 정리합니다.')}</p>
<fieldset class="sec">
	<legend>{t('검색 인덱스')}</legend>
	<p class="desc">{t('아직 색인되지 않은 스냅샷을 다시 색인합니다.')}</p>
	<div class="btn-row">
		<button disabled={busy || reindexRunning} onclick={startReindex} aria-busy={reindexRunning}>
			{#if reindexRunning}<Spinner />{/if}{t('검색 인덱스 전체 재색인')}
		</button>
		{#if reindexRunning}<span class="muted">{t('재색인 중')} {reindexDone}/{reindexTotal}</span>{/if}
	</div>
</fieldset>
<fieldset class="sec">
	<legend>{t('저장공간 최적화')}</legend>
	<p class="desc">{t('압축·자원 공유로 저장공간을 줄입니다 (내용은 그대로).')}</p>
	<button disabled={busy} onclick={compact} aria-busy={pending === 'compact'}>
		{#if pending === 'compact'}<Spinner />{t('최적화 중…')}{:else}{t('저장공간 최적화')}{/if}
	</button>
</fieldset>

<!-- ── 아카이브 설정 ── -->
<h3 class="group">{t('아카이브 설정')}</h3>
<p class="desc">{t('아카이빙·크롤·문서 수집·로컬 네트워크 동작을 설정합니다.')}</p>

<fieldset class="sec">
	<legend>{t('사이트 아카이브 기본값')}</legend>
	<p class="desc">{t('사이트 전체 아카이브(크롤)의 기본 범위·간격입니다.')}</p>
	<label>{t('최대 페이지')} <input type="number" bind:value={crawlMaxPages} /></label>
	<label>{t('최대 깊이')} <input type="number" bind:value={crawlMaxDepth} /></label>
	<label>{t('지연(초)')} <input type="number" bind:value={crawlDelay} /></label>
	<label>{t('재시도 대기(초, 쉼표)')} <input type="text" bind:value={crawlBackoff} /></label>
	<button disabled={busy} onclick={() => save('/system/crawl-settings', { crawl_max_pages: crawlMaxPages, crawl_max_depth: crawlMaxDepth, crawl_delay: crawlDelay, crawl_retry_backoff: crawlBackoff })}>{t('저장')}</button>
</fieldset>

<fieldset class="sec">
	<legend>{t('캡처')}</legend>
	<p class="desc">{t('스냅샷을 찍을 때의 추가 캡처 동작입니다.')}</p>
	<label class="ck"><input type="checkbox" bind:checked={mobileShot} /> {t('모바일 스크린샷도 저장')}</label>
	<button disabled={busy} onclick={() => save('/system/capture-settings', { mobile_screenshot_enabled: mobileShot })}>{t('저장')}</button>
</fieldset>

<fieldset class="sec">
	<legend>{t('확장 자격증명')}</legend>
	<p class="desc">{t('확장이 보낸 1회성 로그인 자격증명의 보관 시간입니다.')}</p>
	<label>{t('보관 시간(시간)')} <input type="number" bind:value={credTtl} min={s.ext_credential_ttl_limits.min} max={s.ext_credential_ttl_limits.max} /></label>
	<button disabled={busy} onclick={() => save('/system/credential-settings', { ext_credential_ttl_hours: credTtl })}>{t('저장')}</button>
</fieldset>

<fieldset class="sec">
	<legend>{t('문서 아카이브 한도')}</legend>
	<p class="desc">{t('페이지가 링크한 문서 파일을 받을 때의 한도입니다.')}</p>
	<label>{t('스냅샷당 수')} <input type="number" bind:value={docCount} /></label>
	<label>{t('개당 크기(MB)')} <input type="number" bind:value={docMb} /></label>
	<label>{t('다운로드 타임아웃(초)')} <input type="number" bind:value={docTimeout} /></label>
	<button disabled={busy} onclick={() => save('/system/document-settings', { document_max_count: docCount, document_max_mb: docMb, document_fetch_timeout: docTimeout })}>{t('저장')}</button>
</fieldset>

<fieldset class="sec">
	<legend>{t('로컬 네트워크 태그')}</legend>
	<p class="desc">{t('사설 IP(로컬 네트워크) 주소를 아카이빙할 때 붙이는 태그입니다.')}</p>
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

<!-- ── 사용자 설정 ── -->
<h3 class="group">{t('사용자 설정')}</h3>
<p class="desc">{t('회원 가입과 이메일 본인 인증 정책입니다.')}</p>
<fieldset class="sec">
	<legend>{t('가입 설정')}</legend>
	<p class="desc">{t('회원 가입 허용 여부와 가입 시 초기 권한입니다.')}</p>
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
	<p class="desc">{t('패스워드 계정이 로그인 전에 메일로 이메일을 검증하게 합니다.')}</p>
	<label class="ck"><input type="checkbox" bind:checked={evEnabled} /> {t('사용')}</label>
	<label>{t('코드 만료(분)')} <input type="number" bind:value={evTtl} min={s.email_verification_ttl_limits.min} max={s.email_verification_ttl_limits.max} /></label>
	<button disabled={busy} onclick={() => save('/system/email-verification-settings', { email_verification_enabled: evEnabled, email_verification_ttl_minutes: evTtl })}>{t('저장')}</button>
</fieldset>
<fieldset class="sec">
	<legend>{t('인증 보호 (무차별 대입 방어)')}</legend>
	<p class="desc">{t('로그인·2단계 인증·이메일 코드의 시도 횟수를 제한합니다. 한도를 넘으면 잠시 차단됩니다.')}</p>
	<label class="ck"><input type="checkbox" bind:checked={atEnabled} /> {t('사용')}</label>
	<label>{t('로그인 시도 한도(이메일별)')} <input type="number" bind:value={atLoginLimit} min={s.auth_throttle_limits.limit_min} max={s.auth_throttle_limits.limit_max} /></label>
	<label>{t('로그인 시도 한도(IP별)')} <input type="number" bind:value={atLoginIpLimit} min={s.auth_throttle_limits.limit_min} max={s.auth_throttle_limits.limit_max} /></label>
	<label>{t('로그인 카운트 창(분)')} <input type="number" bind:value={atLoginWindow} min={s.auth_throttle_limits.window_min} max={s.auth_throttle_limits.window_max} /></label>
	<label>{t('2단계 인증 시도 한도')} <input type="number" bind:value={atTotpLimit} min={s.auth_throttle_limits.limit_min} max={s.auth_throttle_limits.limit_max} /></label>
	<label>{t('이메일 코드 오답 한도')} <input type="number" bind:value={atEmailVerifyLimit} min={s.auth_throttle_limits.limit_min} max={s.auth_throttle_limits.limit_max} /></label>
	<label>{t('이메일 코드 재발송 한도(시간당)')} <input type="number" bind:value={atEmailResendLimit} min={s.auth_throttle_limits.limit_min} max={s.auth_throttle_limits.limit_max} /></label>
	<button disabled={busy} onclick={() => save('/system/auth-throttle-settings', { auth_throttle_enabled: atEnabled, login_limit: atLoginLimit, login_ip_limit: atLoginIpLimit, login_window_minutes: atLoginWindow, totp_limit: atTotpLimit, email_verify_limit: atEmailVerifyLimit, email_resend_limit: atEmailResendLimit })}>{t('저장')}</button>
</fieldset>

<!-- ── 서버 환경설정 ── -->
<h3 class="group">{t('서버 환경설정')}</h3>
<p class="desc">{t('메일 발송과 API 키 등 서버 연동 설정입니다.')}</p>
<fieldset class="sec">
	<legend>{t('메일(SMTP)')} — {s.smtp_config.enabled ? t('사용 중') : t('미설정')}</legend>
	<p class="desc">{t('초대·이메일 인증 메일을 보내는 SMTP 서버입니다.')}</p>
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
<p class="desc"><a href="{base}/system/api-keys">{t('API 키 관리로 이동')}</a></p>

<!-- ── 위험 구역 ── -->
<h3 class="group danger-title">{t('위험 구역')}</h3>
<p class="desc">{t('데이터 전체를 바꾸는 작업입니다 — 신중히 사용하세요.')}</p>
<fieldset class="sec danger">
	<legend>{t('데이터 관리')}</legend>
	<p class="desc">{t('전체 백업·복원과 아카이브 내보내기·가져오기입니다.')}</p>
	<div class="btn-row">
		<button disabled={busy} onclick={() => doDownload('/system/backup')} aria-busy={pending === '/system/backup'}>
			{#if pending === '/system/backup'}<Spinner />{t('백업 준비중…')}{:else}{t('전체 백업 다운로드')}{/if}
		</button>
		<button disabled={busy} onclick={() => doDownload('/system/export')} aria-busy={pending === '/system/export'}>
			{#if pending === '/system/export'}<Spinner />{t('내보내기 준비중…')}{:else}{t('아카이브 내보내기')}{/if}
		</button>
	</div>
	<label>{t('백업 복원')}
		<input type="file" accept=".ccg.backup" disabled={busy}
			onchange={(e) => uploadFile(e, '/system/restore', t('정말 복원하시겠습니까? 현재 데이터가 백업 시점으로 교체됩니다.'))} />
	</label>
	<label>{t('가져오기 모드')}
		<select bind:value={importMode}>
			<option value="merge">{t('병합')}</option>
			<option value="overwrite">{t('덮어쓰기')}</option>
		</select>
	</label>
	<label>{t('아카이브 가져오기')}
		<input type="file" accept=".ccg.export" disabled={busy}
			onchange={(e) => uploadFile(e, '/system/import', '', { mode: importMode })} />
	</label>
</fieldset>

<fieldset class="sec danger">
	<legend>{t('다른 춘추관으로 이전')} — {s.migration_mode ? t('이전 모드 켜짐') : t('이전 모드 꺼짐')}</legend>
	<p class="desc">{t('다른 춘추관 인스턴스로 전체 데이터를 옮길 때 켭니다 — 켜면 아카이빙이 중단됩니다.')}</p>
	{#if migrationToken}
		<p class="mono mtoken">{migrationToken}</p>
		<p class="muted">{t('이 토큰은 다시 표시되지 않습니다 — 받는 쪽에 안전하게 전달하세요.')}</p>
	{/if}
	{#if s.migration_mode}
		<div class="btn-row">
			<button disabled={busy} onclick={() => migrationAction('regenerate')}>{t('토큰 재발급')}</button>
			<button disabled={busy} onclick={() => migrationAction('disable')}>{t('이전 모드 끄기')}</button>
		</div>
	{:else}
		<button disabled={busy} onclick={() => migrationAction('enable')}>{t('이전 모드 켜기')}</button>
	{/if}
</fieldset>

<style>
	/* 그룹 제목 — 설정 섹션들을 묶는 상단 헤더 */
	h3.group {
		font-size: 13px;
		font-weight: 700;
		text-transform: none;
		letter-spacing: 0;
		color: var(--fg);
		border-bottom: 1px solid var(--border);
		padding-bottom: 4px;
		margin: 28px 0 4px;
	}
	h3.group.danger-title {
		color: var(--red-text);
		border-color: var(--red);
	}
	.desc {
		font-size: 12px;
		color: var(--muted);
		margin: 0 0 8px;
		max-width: 560px;
	}
	/* 저장 용량 미터 차트 */
	.meter-box {
		max-width: 560px;
		margin: 8px 0 4px;
	}
	.meter-head {
		display: flex;
		justify-content: space-between;
		font-size: 12px;
		margin-bottom: 4px;
	}
	.meter {
		display: flex;
		height: 14px;
		border-radius: 7px;
		overflow: hidden;
		background: var(--bg-soft);
	}
	.meter .seg {
		height: 100%;
	}
	.seg-db {
		background: var(--blue);
	}
	.seg-sites {
		background: var(--green);
	}
	.seg-res {
		background: var(--amber);
	}
	.seg-docs {
		background: var(--gray);
	}
	.legend-list {
		list-style: none;
		padding: 0;
		margin: 8px 0 0;
		display: flex;
		flex-wrap: wrap;
		gap: 4px 16px;
		font-size: 12px;
	}
	.legend-list li {
		display: flex;
		align-items: center;
		gap: 6px;
	}
	.legend-list .dot {
		width: 10px;
		height: 10px;
		border-radius: 2px;
		display: inline-block;
	}
	.sec.danger {
		border-color: var(--red);
	}
	.sec.danger legend {
		color: var(--red-text);
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
		flex-wrap: wrap;
		justify-content: space-between;
		align-items: center;
		gap: 8px;
	}
	.sec label.ck {
		justify-content: flex-start;
	}
	.sec label input[type='text'],
	.sec label select {
		flex: 1 1 180px;
		min-width: 0;
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
		flex-wrap: wrap;
		gap: 8px;
	}
	/* 로딩 시 버튼 안 스피너+텍스트 정렬 (export 페이지의 .export-link button 패턴) */
	.btn-row button,
	.sec > button {
		display: inline-flex;
		align-items: center;
		gap: 6px;
	}
	.mtoken {
		background: var(--code-bg, var(--border));
		border-radius: 4px;
		padding: 8px 10px;
		font-size: 12px;
		word-break: break-all;
	}
</style>
