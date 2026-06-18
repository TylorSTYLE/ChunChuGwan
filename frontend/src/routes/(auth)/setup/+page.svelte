<script lang="ts">
	import { onMount } from 'svelte';
	import { base } from '$app/paths';
	import { goto, invalidateAll } from '$app/navigation';
	import { t } from '$lib/i18n';
	import { api, ApiError } from '$lib/api';
	import { afterAuth } from '$lib/auth';
	import type { LoginResult, MigrationStatus } from '$lib/types';

	let { data }: { data: { migration: MigrationStatus } } = $props();
	const m = $derived(data.migration);
	const ACTIVE = ['connecting', 'manifest', 'downloading', 'restoring'];
	const active = $derived(ACTIVE.includes(m.status));
	const ongoing = $derived(m.status && m.status !== 'idle');

	let error = $state('');
	let busy = $state(false);

	// 관리자 등록
	let email = $state('');
	let password = $state('');
	// 네트워크 이전
	let sourceUrl = $state('');
	let token = $state('');
	let restoreFiles = $state<FileList | null>(null);

	async function createAdmin(e: SubmitEvent) {
		e.preventDefault();
		busy = true;
		error = '';
		try {
			const res = await api<LoginResult>('/auth/setup', {
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

	async function restore(e: SubmitEvent) {
		e.preventDefault();
		if (!restoreFiles || !restoreFiles[0]) return;
		busy = true;
		error = '';
		try {
			const fd = new FormData();
			fd.set('file', restoreFiles[0]);
			await api('/auth/setup/restore', { method: 'POST', body: fd, redirectOn401: false });
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
				redirectOn401: false
			});
			await invalidateAll();
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
		} finally {
			busy = false;
		}
	}

	const retryMigrate = () =>
		runMigrateAction('/auth/setup/migrate/retry');
	const finishMigrate = () => {
		if (!confirm(t('빠진 파일을 무시하고 이전을 마무리할까요? 받은 데이터로 서비스를 시작합니다.')))
			return;
		runMigrateAction('/auth/setup/migrate/finish');
	};
	async function runMigrateAction(path: string) {
		busy = true;
		error = '';
		try {
			await api(path, { method: 'POST', redirectOn401: false });
			await invalidateAll();
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
		} finally {
			busy = false;
		}
	}

	// 이전 진행 중이면 상태를 폴링한다 — done 이면 로그인, 상호작용 상태면 다시 그린다.
	onMount(() => {
		if (!active) return;
		const timer = setInterval(async () => {
			try {
				const s = await api<MigrationStatus>('/auth/setup/migrate/status', {
					redirectOn401: false
				});
				if (s.status === 'done') {
					clearInterval(timer);
					await goto(`${base}/login`, { invalidateAll: true });
				} else if (s.status === 'partial' || s.status === 'error') {
					clearInterval(timer);
					await invalidateAll();
				}
			} catch {
				/* 일시 오류 — 다음 폴링에서 회복 */
			}
		}, 1500);
		return () => clearInterval(timer);
	});
</script>

<div class="auth-card wide">
	<h2>{t('최초 설정')}</h2>
	<p class="muted">
		{t('최초 구동입니다. 새 관리자 계정을 만들거나, 백업 파일에서 복원하거나, 다른 춘추관에서 네트워크로 데이터를 가져올 수 있습니다.')}
	</p>
	{#if error}<div class="error">{error}</div>{/if}

	{#if ongoing}
		<div class="migrate-box">
			<p><b>{t('네트워크 이전')}</b> — {m.status}</p>
			{#if active}
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
					<button type="button" onclick={retryMigrate} disabled={busy}>{t('전체 재시도')}</button>
					<button type="button" onclick={finishMigrate} disabled={busy}>{t('무시하고 이전 종료')}</button>
				</div>
			{/if}
		</div>
	{/if}

	{#if !active}
		<h3>{t('관리자 계정 생성')}</h3>
		<form onsubmit={createAdmin}>
			<label
				>{t('관리자 이메일')}
				<input type="email" bind:value={email} required autocomplete="username" />
			</label>
			<label
				>{t('패스워드')} <span class="muted">{t('(8자 이상)')}</span>
				<input type="password" bind:value={password} minlength="8" required autocomplete="new-password" />
			</label>
			<button type="submit" disabled={busy}>{t('등록')}</button>
		</form>

		<h3>{t('백업 파일에서 복원')}</h3>
		<p class="muted sm">
			{t('전체 백업(tar.gz)을 올려 그 시점 상태로 복원합니다. 복원 후에는 백업의 계정으로 로그인합니다.')}
		</p>
		<form onsubmit={restore}>
			<input type="file" accept=".tar.gz,.tgz,application/gzip" bind:files={restoreFiles} required />
			<button type="submit" disabled={busy || !restoreFiles?.length}>{t('복원')}</button>
		</form>

		<h3>{t('다른 춘추관에서 이전')}</h3>
		<p class="muted sm">
			{t('이전(마이그레이션) 모드를 켠 다른 춘추관의 주소와 발급된 토큰을 입력하면 모든 데이터를 가져옵니다. 받는 쪽은 같은 WCCG_SECRET_KEY 를 써야 외부 사이트 자격증명을 복호화할 수 있습니다.')}
		</p>
		<form onsubmit={startMigrate}>
			<label
				>{t('소스 주소')} <span class="muted">{t('(예: https://NAS주소:8765)')}</span>
				<input type="url" bind:value={sourceUrl} placeholder="https://…" required />
			</label>
			<label
				>{t('이전 토큰')}
				<input type="text" bind:value={token} required autocomplete="off" />
			</label>
			<button type="submit" disabled={busy}>{t('이전 시작')}</button>
		</form>
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
	.migrate-box {
		background: var(--amber-bg);
		color: var(--amber);
		border: 1px solid var(--amber);
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
	.migrate-box .warn {
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
