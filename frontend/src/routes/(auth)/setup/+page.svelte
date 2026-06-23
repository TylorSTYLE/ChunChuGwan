<script lang="ts">
	import { onMount } from 'svelte';
	import { base } from '$app/paths';
	import { goto, invalidateAll } from '$app/navigation';
	import { t } from '$lib/i18n';
	import { api, ApiError } from '$lib/api';
	import { afterAuth } from '$lib/auth';
	import type { LoginResult, MigrationStatus, RecoveryStatus } from '$lib/types';
	import { Button } from '$lib/components/ui/button';
	import { Input } from '$lib/components/ui/input';

	let {
		data
	}: {
		data: {
			migration: MigrationStatus;
			tokenRequired: boolean;
			kase: string;
			flags: { has_archive_data: boolean; s3_db_backup: boolean };
			recovery: RecoveryStatus | null;
		};
	} = $props();
	const m = $derived(data.migration);
	const tokenRequired = $derived(data.tokenRequired);
	const kase = $derived(data.kase);
	const MIGRATE_ACTIVE = ['connecting', 'manifest', 'downloading', 'restoring'];
	const migrateActive = $derived(MIGRATE_ACTIVE.includes(m.status));
	const ongoing = $derived(m.status && m.status !== 'idle');

	// 폴링으로 덮어쓴 값이 있으면 그것을, 없으면 로더의 초기 상태를 쓴다(prop 반응성 유지).
	let recOverride = $state<RecoveryStatus | null>(null);
	const rec = $derived(recOverride ?? data.recovery);
	const recActive = $derived(!!rec && (rec.status === 'scanning' || rec.status === 'rebuilding'));

	let error = $state('');
	let busy = $state(false);

	// 관리자 등록
	let email = $state('');
	let password = $state('');
	// 네트워크 이전
	let sourceUrl = $state('');
	let token = $state('');
	let restoreFiles = $state<FileList | null>(null);
	// 최초 설정 보호 토큰 (WCCG_SETUP_TOKEN 설정 시 요구 — F3)
	let setupToken = $state('');
	const setupHeaders = (): Record<string, string> =>
		tokenRequired ? { 'X-Setup-Token': setupToken.trim() } : {};

	async function createAdmin(e: SubmitEvent) {
		e.preventDefault();
		busy = true;
		error = '';
		try {
			const res = await api<LoginResult>('/auth/setup', {
				method: 'POST',
				body: JSON.stringify({ email: email.trim(), password }),
				headers: setupHeaders(),
				redirectOn401: false
			});
			await afterAuth(res.status);
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
			busy = false;
		}
	}

	async function restore(e: SubmitEvent) {
		e.preventDefault();
		if (!restoreFiles || !restoreFiles[0]) return;
		busy = true;
		error = '';
		try {
			const fd = new FormData();
			fd.set('file', restoreFiles[0]);
			await api('/auth/setup/restore', {
				method: 'POST',
				body: fd,
				headers: setupHeaders(),
				redirectOn401: false
			});
			await goto(`${base}/login`, { invalidateAll: true });
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
			busy = false;
		}
	}

	async function startMigrate(e: SubmitEvent) {
		e.preventDefault();
		busy = true;
		error = '';
		try {
			await api('/auth/setup/migrate', {
				method: 'POST',
				body: JSON.stringify({ source_url: sourceUrl.trim(), token: token.trim() }),
				headers: setupHeaders(),
				redirectOn401: false
			});
			await invalidateAll();
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
		} finally {
			busy = false;
		}
	}

	const retryMigrate = () => runMigrateAction('/auth/setup/migrate/retry');
	const finishMigrate = () => {
		if (!confirm(t('빠진 파일을 무시하고 이전을 마무리할까요? 받은 데이터로 서비스를 시작합니다.')))
			return;
		runMigrateAction('/auth/setup/migrate/finish');
	};
	async function runMigrateAction(path: string) {
		busy = true;
		error = '';
		try {
			await api(path, { method: 'POST', headers: setupHeaders(), redirectOn401: false });
			await invalidateAll();
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
		} finally {
			busy = false;
		}
	}

	// ── S3 DB백업 복원 (완전 — 사용자 포함 → 로그인) ──
	async function restoreS3() {
		if (
			!confirm(
				t('S3 DB 백업에서 복원하면 사용자 계정을 포함한 전체 인덱스가 백업 시점으로 채워집니다. 계속할까요?')
			)
		)
			return;
		busy = true;
		error = '';
		try {
			await api('/auth/setup/restore-s3', {
				method: 'POST',
				headers: setupHeaders(),
				redirectOn401: false
			});
			await goto(`${base}/login`, { invalidateAll: true });
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
			busy = false;
		}
	}

	// ── 복구모드 (부분 — 복구분 authenticated=1, 완료 후 관리자 생성) ──
	async function startRecover() {
		busy = true;
		error = '';
		try {
			await api('/auth/setup/recover', {
				method: 'POST',
				headers: setupHeaders(),
				redirectOn401: false
			});
			recOverride = await api<RecoveryStatus>('/auth/setup/recover/status', { redirectOn401: false });
			pollRecover();
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
		} finally {
			busy = false;
		}
	}

	const retryRecover = () => runRecoverAction('/auth/setup/recover/retry');
	async function runRecoverAction(path: string) {
		busy = true;
		error = '';
		try {
			await api(path, { method: 'POST', headers: setupHeaders(), redirectOn401: false });
			recOverride = await api<RecoveryStatus>('/auth/setup/recover/status', { redirectOn401: false });
			pollRecover();
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
		} finally {
			busy = false;
		}
	}

	// 복구 진행 폴링 (migrate 폴링 미러) — done 이면 재분류(데이터 보존 → 관리자 생성).
	let recTimer: ReturnType<typeof setInterval> | null = null;
	function clearRecTimer() {
		if (recTimer) {
			clearInterval(recTimer);
			recTimer = null;
		}
	}
	function pollRecover() {
		if (recTimer) return;
		recTimer = setInterval(async () => {
			try {
				const s = await api<RecoveryStatus>('/auth/setup/recover/status', {
					redirectOn401: false
				});
				recOverride = s;
				if (s.status === 'done') {
					clearRecTimer();
					await invalidateAll(); // 재분류 → data_preserved → 관리자 생성
				} else if (s.status === 'error') {
					clearRecTimer();
				}
			} catch {
				/* 일시 오류 — 다음 폴링에서 회복 */
			}
		}, 1500);
	}

	// 진행 중인 이전/복구가 있으면 상태를 폴링한다.
	onMount(() => {
		let migrateTimer: ReturnType<typeof setInterval> | null = null;
		if (migrateActive) {
			migrateTimer = setInterval(async () => {
				try {
					const s = await api<MigrationStatus>('/auth/setup/migrate/status', {
						redirectOn401: false
					});
					if (s.status === 'done') {
						if (migrateTimer) clearInterval(migrateTimer);
						await goto(`${base}/login`, { invalidateAll: true });
					} else if (s.status === 'partial' || s.status === 'error') {
						if (migrateTimer) clearInterval(migrateTimer);
						await invalidateAll();
					}
				} catch {
					/* 일시 오류 — 다음 폴링에서 회복 */
				}
			}, 1500);
		}
		if (recActive) pollRecover();
		return () => {
			if (migrateTimer) clearInterval(migrateTimer);
			clearRecTimer();
		};
	});
</script>

{#snippet adminForm()}
	<h3>{t('관리자 계정 생성')}</h3>
	<form onsubmit={createAdmin}>
		<label
			>{t('관리자 이메일')}
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
		<Button type="submit" disabled={busy} class="mt-1 w-full">{t('등록')}</Button>
	</form>
{/snippet}

{#snippet restoreForm()}
	<h3>{t('백업 파일에서 복원')}</h3>
	<p class="muted sm">
		{t('전체 백업(tar.gz)을 올려 그 시점 상태로 복원합니다. 복원 후에는 백업의 계정으로 로그인합니다.')}
	</p>
	<form onsubmit={restore}>
		<input type="file" accept=".tar.gz,.tgz,application/gzip" bind:files={restoreFiles} required />
		<Button type="submit" disabled={busy || !restoreFiles?.length} class="mt-1 w-full">
			{t('복원')}
		</Button>
	</form>
{/snippet}

{#snippet migrateForm()}
	<h3>{t('다른 춘추관에서 이전')}</h3>
	<p class="muted sm">
		{t('이전(마이그레이션) 모드를 켠 다른 춘추관의 주소와 발급된 토큰을 입력하면 모든 데이터를 가져옵니다. 받는 쪽은 같은 WCCG_SECRET_KEY 를 써야 외부 사이트 자격증명을 복호화할 수 있습니다.')}
	</p>
	<form onsubmit={startMigrate}>
		<label
			>{t('소스 주소')} <span class="muted">{t('(예: https://NAS주소:8765)')}</span>
			<Input type="url" bind:value={sourceUrl} placeholder="https://…" required />
		</label>
		<label
			>{t('이전 토큰')}
			<Input type="text" bind:value={token} required autocomplete="off" />
		</label>
		<Button type="submit" disabled={busy} class="mt-1 w-full">{t('이전 시작')}</Button>
	</form>
{/snippet}

<div class="auth-card wide">
	<h2>{t('최초 설정')}</h2>
	<p class="muted">
		{t('최초 구동입니다. 새 관리자 계정을 만들거나, 백업 파일에서 복원하거나, 다른 춘추관에서 네트워크로 데이터를 가져올 수 있습니다.')}
	</p>
	{#if error}<div class="error">{error}</div>{/if}

	{#if ongoing}
		<div class="migrate-box">
			<p><b>{t('네트워크 이전')}</b> — {m.status}</p>
			{#if migrateActive}
				<p class="sm">{t('받는 중')}: {m.done ?? 0} / {m.total ?? 0}</p>
			{/if}
			{#if m.insecure}
				<p class="sm warn">{t('⚠ 평문 http 연결입니다 — 토큰이 노출될 수 있습니다.')}</p>
			{/if}
			{#if m.status === 'error'}<p class="sm">{m.error}</p>{/if}
			{#if m.status === 'partial'}
				<p class="sm">
					{t('일부 파일을 받지 못했습니다. 전체 재시도하거나, 무시하고 이전을 마무리할 수 있습니다(빠진 스냅샷 파일은 표시되지 않을 수 있습니다).')}
				</p>
				{#if m.failed && m.failed.length > 0}
					<details class="sm">
						<summary>{t('실패한 파일 목록')}</summary>
						<ul>
							{#each m.failed as f}
								<li class="mono">{f.path} <span class="muted">— {f.error}</span></li>
							{/each}
						</ul>
					</details>
				{/if}
				<div class="row">
					<Button variant="outline" size="sm" onclick={retryMigrate} disabled={busy}>
						{t('전체 재시도')}
					</Button>
					<Button variant="outline" size="sm" onclick={finishMigrate} disabled={busy}>
						{t('무시하고 이전 종료')}
					</Button>
				</div>
			{/if}
		</div>
	{/if}

	{#if rec && rec.status !== 'idle'}
		<div class="migrate-box">
			<p><b>{t('복구모드')}</b> — {rec.status}</p>
			{#if recActive}
				<p class="sm">{t('인덱스 재구축 중')}: {rec.done ?? 0} / {rec.total ?? 0}</p>
			{/if}
			{#if rec.status === 'error'}
				<p class="sm">{rec.error}</p>
				<div class="row">
					<Button variant="outline" size="sm" onclick={retryRecover} disabled={busy}>
						{t('복구 재시도')}
					</Button>
				</div>
			{/if}
			{#if rec.status === 'done'}
				<p class="sm">
					{t('복구를 완료했습니다. 복구된 스냅샷은 기본적으로 관리자 전용으로 제한됩니다 — 관리자 계정을 만든 뒤 공개 정책을 선택하세요.')}
				</p>
			{/if}
		</div>
	{/if}

	{#if !migrateActive}
		{#if tokenRequired}
			<label class="setup-token"
				>{t('최초 설정 토큰')} <span class="muted">{t('(WCCG_SETUP_TOKEN)')}</span>
				<Input type="password" bind:value={setupToken} autocomplete="off" />
			</label>
			<p class="muted sm">
				{t('이 서버는 최초 설정 보호 토큰을 요구합니다. 아래 작업에 서버 환경변수 WCCG_SETUP_TOKEN 값을 입력하세요.')}
			</p>
		{/if}

		{#if kase === 'data_preserved'}
			<div class="notice">
				{t('기존 아카이브 데이터를 발견해 보존했습니다. 관리자 계정을 만들어 시작하세요.')}
			</div>
			{@render adminForm()}
			<details class="adv">
				<summary>{t('고급 — 감지된 데이터를 대체하는 작업')}</summary>
				<p class="muted sm warn">
					{t('아래 작업은 방금 감지된 기존 데이터를 대체합니다. 의도한 경우에만 사용하세요.')}
				</p>
				{@render restoreForm()}
				{@render migrateForm()}
			</details>
		{:else if kase === 'restore_s3'}
			<div class="notice">
				{t('S3 에 DB 백업이 있습니다. 복원하면 사용자 계정을 포함한 전체가 백업 시점으로 복구됩니다.')}
			</div>
			<h3>{t('S3 DB 백업에서 복원')}</h3>
			<p class="muted sm">
				{t('S3 db-backups/ 의 최신 백업으로 복원합니다. 완료되면 백업의 계정으로 로그인합니다.')}
			</p>
			<Button onclick={restoreS3} disabled={busy} class="mt-1 w-full">
				{t('S3 DB 백업 복원')}
			</Button>
			<details class="adv">
				<summary>{t('또는 새로 시작 (관리자 생성)')}</summary>
				{@render adminForm()}
			</details>
		{:else if kase === 'recover_local' || kase === 'recover_s3'}
			{#if rec?.status === 'done'}
				{@render adminForm()}
			{:else}
				<div class="notice">
					{t('아카이브 blob 을 발견했지만 인덱스(DB)가 비어 있습니다. 복구모드로 blob 에서 인덱스를 재구축할 수 있습니다 — 복구된 스냅샷은 기본적으로 관리자 전용으로 제한되고, 복구 후 관리자 계정을 만듭니다.')}
				</div>
				<Button onclick={startRecover} disabled={busy || recActive} class="mt-1 w-full">
					{t('복구모드 시작')}
				</Button>
			{/if}
		{:else}
			{@render adminForm()}
			{@render restoreForm()}
			{@render migrateForm()}
		{/if}
	{/if}
</div>

<style>
	.auth-card.wide {
		max-width: 560px;
	}
	.auth-card h3 {
		font-size: 14px;
		margin: 18px 0 6px;
		color: var(--fg);
		text-transform: none;
		letter-spacing: 0;
	}
	.auth-card form {
		margin-bottom: 8px;
	}
	.notice {
		background: var(--bg-soft);
		border: 1px solid var(--border);
		border-radius: 4px;
		padding: 10px 12px;
		margin-bottom: 12px;
		font-size: 13px;
	}
	.adv {
		margin-top: 14px;
		border-top: 1px solid var(--border);
		padding-top: 8px;
	}
	.adv summary {
		font-size: 13px;
		cursor: pointer;
		color: var(--muted);
	}
	.migrate-box {
		background: var(--changed-bg);
		color: var(--changed);
		border: 1px solid var(--changed);
		border-radius: 4px;
		padding: 10px 12px;
		margin-bottom: 12px;
	}
	.migrate-box p {
		margin: 0 0 6px;
	}
	.migrate-box .sm,
	.auth-card .sm {
		font-size: 12px;
	}
	.migrate-box .warn,
	.auth-card .warn {
		font-weight: 600;
	}
	.migrate-box .row {
		display: flex;
		gap: 8px;
		margin-top: 8px;
	}
	.migrate-box ul {
		margin: 6px 0 0;
		padding-left: 18px;
		max-height: 160px;
		overflow: auto;
	}
	.auth-card input[type='file'] {
		border: none;
		padding-left: 0;
	}
</style>
