<script lang="ts">
	import { onMount } from 'svelte';
	import { base } from '$app/paths';
	import { invalidateAll } from '$app/navigation';
	import { t } from '$lib/i18n';
	import { filesize } from '$lib/format';
	import { api, ApiError, download } from '$lib/api';
	import type {
		SystemOverview,
		StorageStatus,
		DbBackupStatus,
		RecoveryStatus,
		StorageUsage
	} from '$lib/types';
	import AlertBox from '$lib/components/AlertBox.svelte';
	import Spinner from '$lib/components/Spinner.svelte';
	import { Button } from '$lib/components/ui/button';
	import { Input } from '$lib/components/ui/input';
	import { Textarea } from '$lib/components/ui/textarea';

	let { data }: { data: { sys: SystemOverview } } = $props();
	const s = $derived(data.sys);

	// 저장 사용량 분해 — sites/resources/documents 전체 스캔이라 페이지 진입을
	// 막지 않도록 /system/usage 로 분리해 마운트 후 비동기로 받는다(도착 전 null).
	let diskUsage = $state<Record<string, number> | null>(null);

	// 저장 용량 미터 차트 — 각 영역이 전체에서 차지하는 비율
	const usageTotal = $derived(
		((diskUsage?.db ?? 0) +
			(diskUsage?.sites ?? 0) +
			(diskUsage?.resources ?? 0) +
			(diskUsage?.documents ?? 0)) || 1
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
	let limitMaxPages = $state(0);
	let limitMaxDepth = $state(0);
	let limitMaxDelay = $state(0);
	let credTtl = $state(0);
	let mobileShot = $state(false);
	let trashEnabled = $state(true);
	let trashRetention = $state(30);
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

	// AI 자동 챌린지 해결(B)
	let aiEnabled = $state(false);
	let aiBaseUrl = $state('');
	let aiModel = $state('');
	let aiApiKey = $state('');
	let aiClearKey = $state(false);
	let aiActionPrompt = $state('');
	let aiVerdictPrompt = $state('');
	let aiMaxRounds = $state(3);
	let aiVerdictDelay = $state(1500);
	let aiMaxActions = $state(10);
	let aiRequestTimeout = $state(30);
	let aiSuccessRecheck = $state(true);

	// 백업·복원·가져오기
	let importMode = $state('merge');

	// 유지보수 — 재색인 진행
	let reindexRunning = $state(false);
	let reindexDone = $state(0);
	let reindexTotal = $state(0);

	// 유지보수 — 아카이브 링크 교정 진행
	let linkRepairRunning = $state(false);
	let linkRepairDone = $state(0);
	let linkRepairTotal = $state(0);
	let linkRepairPending = $state(0);

	// 데이터 이전(마이그레이션) — 발급 토큰은 1회만 표시
	let migrationToken = $state('');

	// 스토리지(blob 백엔드) 마이그레이션 — 서버 status 가 진행/정리대기의 정본.
	let storage = $state<StorageStatus | null>(null);
	const storageRunning = $derived(
		storage?.status === 'manifest' || storage?.status === 'copying'
	);
	const migrateDir = $derived(
		storage?.active_backend === 's3' ? t('S3 → 로컬') : t('로컬 → S3')
	);
	const migratePct = $derived(
		storage?.total ? `${Math.round(((storage.done ?? 0) / storage.total) * 100)}%` : '0%'
	);
	// 처리 속도(파일/초) — 폴링 사이 done 증가분으로 추정.
	let migrateRate = $state(0);
	let lastDone = 0;
	let lastTick = 0;
	function directionLabel(dir?: string): string {
		if (dir === 'local_to_s3') return t('로컬 → S3');
		if (dir === 's3_to_local') return t('S3 → 로컬');
		return dir ?? '';
	}

	// S3 DB 백업 — 서버 status 가 마지막 백업·목록·설정의 정본. 빈번 폴링 없이
	// 진입 시 1회 로드 + [지금 백업] 후 갱신.
	let dbBackup = $state<DbBackupStatus | null>(null);
	let dbbInterval = $state(24);
	let dbbKeep = $state(14);

	// 복구-선택 — 서버 status 의 restricted_count 가 정본(새로고침/세션 간 영속).
	// 복구된 스냅샷이 아직 제한(authenticated=1)인 동안에만 배너가 뜬다.
	let recovery = $state<RecoveryStatus | null>(null);
	const recoveryPending = $derived((recovery?.restricted_count ?? 0) > 0);

	// 저장 사용량 — S3 모드에서 로컬/Object Storage 분리. GET 은 캐시만(S3 미호출),
	// [업데이트] 만 명시 스캔. 로컬 분해는 diskUsage(/system/usage) 로 폴백.
	const isS3 = $derived(s.storage_backend === 's3');
	let usage = $state<StorageUsage | null>(null);
	$effect(() => {
		if (dbBackup) {
			dbbInterval = dbBackup.interval_hours;
			dbbKeep = dbBackup.keep;
		}
	});

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
		limitMaxPages = s.crawl_limits.max_pages;
		limitMaxDepth = s.crawl_limits.max_depth;
		limitMaxDelay = s.crawl_limits.max_delay;
		credTtl = s.ext_credential_ttl_hours;
		mobileShot = s.mobile_screenshot_enabled;
		trashEnabled = s.trash_enabled;
		trashRetention = s.trash_retention_days;
		docCount = s.document_limits.max_count;
		docMb = s.document_limits.max_mb;
		docTimeout = s.document_limits.timeout_seconds;
		smtpHost = s.smtp_config.host;
		smtpPort = s.smtp_config.port;
		smtpUser = s.smtp_config.user;
		smtpFrom = s.smtp_config.sender;
		smtpTls = s.smtp_config.tls;
		const ai = s.ai_challenge_config;
		aiEnabled = ai.enabled;
		aiBaseUrl = ai.base_url;
		aiModel = ai.model;
		aiActionPrompt = ai.action_prompt;
		aiVerdictPrompt = ai.verdict_prompt;
		aiMaxRounds = ai.max_rounds;
		aiVerdictDelay = ai.verdict_delay_ms;
		aiMaxActions = ai.max_actions;
		aiRequestTimeout = ai.request_timeout;
		aiSuccessRecheck = ai.success_recheck;
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

	async function saveAiChallenge() {
		const ok = await save('/system/ai-challenge-settings', {
			ai_challenge_enabled: aiEnabled,
			ai_challenge_base_url: aiBaseUrl,
			ai_challenge_model: aiModel,
			ai_challenge_api_key: aiApiKey,
			ai_challenge_clear_api_key: aiClearKey,
			ai_challenge_action_prompt: aiActionPrompt,
			ai_challenge_verdict_prompt: aiVerdictPrompt,
			ai_challenge_max_rounds: aiMaxRounds,
			ai_challenge_verdict_delay_ms: aiVerdictDelay,
			ai_challenge_max_actions: aiMaxActions,
			ai_challenge_request_timeout: aiRequestTimeout,
			ai_challenge_success_recheck: aiSuccessRecheck
		});
		if (ok) {
			aiApiKey = '';
			aiClearKey = false;
		}
	}

	function resetAiActionPrompt() {
		aiActionPrompt = s.ai_challenge_config.default_action_prompt;
	}

	function resetAiVerdictPrompt() {
		aiVerdictPrompt = s.ai_challenge_config.default_verdict_prompt;
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

	async function pollLinkRepair() {
		try {
			const s = await api<{
				running: boolean;
				done: number;
				total: number;
				pending: number;
				error: string | null;
			}>('/system/links/repair/status');
			linkRepairDone = s.done;
			linkRepairTotal = s.total;
			linkRepairPending = s.pending;
			if (s.running) {
				setTimeout(pollLinkRepair, 1000);
			} else {
				linkRepairRunning = false;
				notice = s.error
					? `${t('링크 교정 실패')}: ${s.error}`
					: t('링크 교정을 완료했습니다.');
				await invalidateAll();
			}
		} catch (err) {
			linkRepairRunning = false;
			error = err instanceof ApiError ? err.message : String(err);
		}
	}

	async function startLinkRepair() {
		error = '';
		notice = '';
		try {
			await api('/system/links/repair', { method: 'POST' });
			linkRepairRunning = true;
			linkRepairDone = 0;
			linkRepairTotal = 0;
			pollLinkRepair();
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
		}
	}

	let storagePolling = false;
	async function loadStorage() {
		try {
			storage = await api<StorageStatus>('/system/storage/status');
			// 화면을 떠났다 돌아와도(컴포넌트 재마운트) 진행 중이면 폴링을 재개한다 —
			// 마이그레이션은 서버 백그라운드 스레드라 멈추지 않으나 폴링이 끊겨
			// '중단된 것처럼' 보였다. 중복 폴링 루프는 storagePolling 가드로 막는다.
			if (
				!storagePolling &&
				(storage.status === 'manifest' || storage.status === 'copying')
			) {
				pollStorage();
			}
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
		}
	}

	// 재색인 폴링(pollReindex) 미러 — 진행 중이면 setTimeout 으로 재폴링.
	let storageTimer: ReturnType<typeof setTimeout> | null = null;
	async function pollStorage() {
		storagePolling = true;
		try {
			const st = await api<StorageStatus>('/system/storage/status');
			storage = st;
			if (st.status === 'manifest' || st.status === 'copying') {
				const now = Date.now();
				if (lastTick && st.status === 'copying') {
					const dt = (now - lastTick) / 1000;
					if (dt > 0) migrateRate = Math.max(0, ((st.done ?? 0) - lastDone) / dt);
				}
				lastDone = st.done ?? 0;
				lastTick = now;
				storageTimer = setTimeout(pollStorage, 1000);
				return;
			} else {
				storagePolling = false;
				migrateRate = 0;
				if (st.status === 'error') {
					notice = `${t('마이그레이션 실패')}: ${st.error ?? ''}`;
				} else if (st.status === 'partial') {
					notice = t('일부 파일이 실패했습니다 — 재시도하세요.');
				} else if (st.status === 'done') {
					notice = t('마이그레이션을 완료했습니다.');
				}
				await invalidateAll(); // 활성 백엔드·용량이 바뀌었을 수 있다
			}
		} catch (err) {
			storagePolling = false;
			error = err instanceof ApiError ? err.message : String(err);
		}
	}

	async function startMigration() {
		if (!confirm(t('마이그레이션 중에는 캡처·스케줄·크롤이 중지됩니다. 진행하시겠습니까?'))) return;
		error = '';
		notice = '';
		try {
			await api('/system/storage/migrate/start', { method: 'POST' });
			lastDone = 0;
			lastTick = 0;
			migrateRate = 0;
			pollStorage();
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
		}
	}

	async function retryMigration() {
		error = '';
		notice = '';
		try {
			await api('/system/storage/migrate/retry', { method: 'POST' });
			lastDone = 0;
			lastTick = 0;
			migrateRate = 0;
			pollStorage();
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
		}
	}

	async function confirmCleanup() {
		error = '';
		notice = '';
		try {
			await api('/system/storage/cleanup/confirm', { method: 'POST' });
			notice = t('원본 정리를 확인했습니다.');
			await loadStorage();
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
		}
	}

	async function loadDbBackup() {
		try {
			dbBackup = await api<DbBackupStatus>('/system/db-backup/status');
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
		}
	}

	async function runDbBackup() {
		busy = true;
		pending = 'db-backup';
		error = '';
		notice = '';
		try {
			dbBackup = await api<DbBackupStatus>('/system/db-backup/run', { method: 'POST' });
			notice = t('DB 백업을 완료했습니다.');
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
		} finally {
			busy = false;
			pending = '';
		}
	}

	async function saveDbBackupSettings() {
		if (await save('/system/db-backup/settings', { interval_hours: dbbInterval, keep: dbbKeep })) {
			await loadDbBackup();
		}
	}

	async function loadRecovery() {
		try {
			recovery = await api<RecoveryStatus>('/system/recovery/status');
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
		}
	}

	async function exposeAllRecovered() {
		if (
			!confirm(
				t('복구된 스냅샷을 모두 전체 공개로 바꿀까요? 로그인 캡처였을 수 있어 비공개 정보가 노출될 수 있습니다.')
			)
		)
			return;
		busy = true;
		error = '';
		notice = '';
		try {
			const r = await api<{ exposed: number }>('/system/recovery/expose-all', { method: 'POST' });
			notice = t('복구 스냅샷 {n}개를 전체 공개로 바꿨습니다.').replace('{n}', String(r.exposed));
			await loadRecovery();
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
		} finally {
			busy = false;
		}
	}

	async function loadDiskUsage() {
		try {
			const r = await api<{ usage: Record<string, number> }>('/system/usage');
			diskUsage = r.usage;
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
		}
	}

	async function loadUsage() {
		try {
			usage = await api<StorageUsage>('/system/storage/usage');
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
		}
	}

	async function scanUsage() {
		if (!confirm(t('모든 객체를 조회하므로 부하가 발생합니다. 진행하시겠습니까?'))) return;
		busy = true;
		pending = 'usage-scan';
		error = '';
		notice = '';
		try {
			usage = await api<StorageUsage>('/system/storage/usage/scan', { method: 'POST' });
			notice = t('사용량을 갱신했습니다.');
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
		} finally {
			busy = false;
			pending = '';
		}
	}

	async function loadLinkRepair() {
		try {
			const s = await api<{
				running: boolean;
				done: number;
				total: number;
				pending: number;
			}>('/system/links/repair/status');
			linkRepairPending = s.pending;
			if (s.running) {
				linkRepairRunning = true;
				linkRepairDone = s.done;
				linkRepairTotal = s.total;
				pollLinkRepair(); // 진행 중이면 폴링 재개(화면 재진입 대응)
			}
		} catch {
			/* 상태 조회 실패는 무시 — 버튼은 그대로 쓸 수 있다 */
		}
	}

	onMount(() => {
		loadStorage(); // 진행 중이면 폴링을 재개한다(화면 재진입 대응)
		loadLinkRepair();
		loadDbBackup();
		loadRecovery();
		loadDiskUsage(); // 저장 용량 미터(분리된 비싼 스캔) — 양 모드 모두
		if (isS3) loadUsage();
		// 화면을 떠나면 폴링 루프를 정리한다(고아 setTimeout 누적 방지).
		return () => {
			if (storageTimer) clearTimeout(storageTimer);
			storagePolling = false;
		};
	});

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

{#if !isS3}
	<div class="meter-box">
		<div class="meter-head">
			<span>{t('저장 용량')}</span>
			<span class="mono muted">{diskUsage ? filesize(usageTotal) : ''}</span>
		</div>
		{#if diskUsage}
			<div class="meter">
				<span class="seg seg-db" style="width:{pct(diskUsage.db)}" title="DB {filesize(diskUsage.db)}"></span>
				<span class="seg seg-sites" style="width:{pct(diskUsage.sites)}" title="{t('사이트')} {filesize(diskUsage.sites)}"></span>
				<span class="seg seg-res" style="width:{pct(diskUsage.resources)}" title="{t('공유 자원')} {filesize(diskUsage.resources)}"></span>
				<span class="seg seg-docs" style="width:{pct(diskUsage.documents)}" title="{t('문서')} {filesize(diskUsage.documents)}"></span>
			</div>
			<ul class="legend-list mono">
				<li><span class="dot seg-db"></span>DB {filesize(diskUsage.db)}</li>
				<li><span class="dot seg-sites"></span>{t('사이트')} {filesize(diskUsage.sites)}</li>
				<li><span class="dot seg-res"></span>{t('공유 자원')} {filesize(diskUsage.resources)}</li>
				<li><span class="dot seg-docs"></span>{t('문서')} {filesize(diskUsage.documents)}</li>
			</ul>
		{:else}
			<p class="desc"><Spinner />{t('저장 용량 계산 중…')}</p>
		{/if}
	</div>
{:else}
	<!-- S3 모드 — 로컬 사용량과 Object Storage 사용량을 분리 -->
	<div class="meter-box">
		<div class="meter-head"><span>{t('로컬 사용량')}</span></div>
		<ul class="legend-list mono">
			<li><span class="dot seg-db"></span>DB {filesize(usage?.local?.db ?? diskUsage?.db ?? 0)}</li>
			<li><span class="dot seg-sites"></span>{t('캐시')} {filesize(usage?.local?.cache ?? diskUsage?.cache ?? 0)}</li>
			<li><span class="dot seg-res"></span>{t('read-through 캐시')} {filesize(usage?.local?.blobcache ?? diskUsage?.blobcache ?? 0)}</li>
		</ul>
	</div>
	<div class="meter-box">
		<div class="meter-head">
			<span>{t('Object Storage 사용량')}</span>
			<span class="mono muted">
				{usage?.s3 ? `${t('마지막 조회')}: ${usage.s3.scanned_at}` : t('미조회')}
			</span>
		</div>
		{#if usage?.s3}
			<ul class="legend-list mono">
				<li><span class="dot seg-sites"></span>{t('사이트')} {filesize(usage.s3.categories.sites ?? 0)}</li>
				<li><span class="dot seg-res"></span>{t('공유 자원')} {filesize(usage.s3.categories.resources ?? 0)}</li>
				<li><span class="dot seg-docs"></span>{t('문서')} {filesize(usage.s3.categories.documents ?? 0)}</li>
				<li><span class="dot seg-db"></span>{t('DB 백업')} {filesize(usage.s3.categories['db-backups'] ?? 0)}</li>
				{#if usage.s3.categories.other}
					<li><span class="dot"></span>{t('기타')} {filesize(usage.s3.categories.other)}</li>
				{/if}
				<li class="muted">{t('총합')} {filesize(usage.s3.total)}</li>
			</ul>
		{:else}
			<p class="desc">{t('아직 조회한 적이 없습니다. [업데이트] 로 S3 사용량을 조회하세요.')}</p>
		{/if}
		<Button variant="outline" size="sm" class="self-start" disabled={busy} onclick={scanUsage} aria-busy={pending === 'usage-scan'}>
			{#if pending === 'usage-scan'}<Spinner />{t('조회 중…')}{:else}{t('업데이트')}{/if}
		</Button>
	</div>
{/if}

<!-- ── 유지관리 ── -->
<h3 class="group">{t('유지관리')}</h3>
<p class="desc">{t('검색 인덱스와 저장공간을 정리합니다.')}</p>
<fieldset class="sec">
	<legend>{t('검색 인덱스')}</legend>
	<p class="desc">{t('아직 색인되지 않은 스냅샷을 다시 색인합니다.')}</p>
	<div class="btn-row">
		<Button variant="outline" size="sm" disabled={busy || reindexRunning || storageRunning} onclick={startReindex} aria-busy={reindexRunning}>
			{#if reindexRunning}<Spinner />{/if}{t('검색 인덱스 전체 재색인')}
		</Button>
		{#if reindexRunning}<span class="muted">{t('재색인 중')} {reindexDone}/{reindexTotal}</span>{/if}
	</div>
</fieldset>
<fieldset class="sec">
	<legend>{t('아카이브 링크 교정')}</legend>
	<p class="desc">{t('구형 스냅샷의 깨진 내부 링크를 아카이브 리졸버로 바로잡습니다 (내용은 그대로).')}</p>
	<div class="btn-row">
		<Button variant="outline" size="sm" disabled={busy || linkRepairRunning || storageRunning} onclick={startLinkRepair} aria-busy={linkRepairRunning}>
			{#if linkRepairRunning}<Spinner />{/if}{t('아카이브 링크 교정')}
		</Button>
		{#if linkRepairRunning}<span class="muted">{t('링크 교정 중')} {linkRepairDone}/{linkRepairTotal}</span>{:else if linkRepairPending}<span class="muted">{t('미교정 스냅샷')} {linkRepairPending}{t('개')}</span>{/if}
	</div>
</fieldset>
<fieldset class="sec">
	<legend>{t('저장공간 최적화')}</legend>
	<p class="desc">{t('압축·자원 공유로 저장공간을 줄입니다 (내용은 그대로).')}</p>
	<Button variant="outline" size="sm" class="self-start" disabled={busy || storageRunning} onclick={compact} aria-busy={pending === 'compact'}>
		{#if pending === 'compact'}<Spinner />{t('최적화 중…')}{:else}{t('저장공간 최적화')}{/if}
	</Button>
</fieldset>

<!-- ── 스토리지 ── -->
<h3 class="group">{t('스토리지')}</h3>
<p class="desc">{t('blob 저장 백엔드를 로컬과 S3 사이에서 옮깁니다. 마이그레이션 중에는 캡처·스케줄·크롤이 중지되고, 0건 실패로 끝나야 활성 백엔드가 전환됩니다.')}</p>

{#if recoveryPending}
	<fieldset class="sec danger">
		<legend>{t('복구 스냅샷 분류 대기')}</legend>
		<p class="desc">
			{t('복구된 스냅샷 {n}개가 보안상 관리자 전용으로 제한되어 있습니다 (로그인 캡처 여부를 알 수 없어 보수적으로 제한).').replace('{n}', String(recovery?.restricted_count ?? 0))}
		</p>
		<p class="desc">
			{t('각 스냅샷 화면에서 개별로 검토해 공개하거나, 아래에서 한 번에 전체 공개로 바꿀 수 있습니다.')}
		</p>
		<Button variant="outline" size="sm" class="self-start" disabled={busy} onclick={exposeAllRecovered}>
			{t('복구 스냅샷 전체 공개')}
		</Button>
	</fieldset>
{/if}

{#if storage}
	<fieldset class="sec">
		<legend>{t('blob 저장 백엔드')}</legend>
		<p class="desc">
			<span class="muted">{t('활성 백엔드')}:</span>
			<span class="mono">{storage.active_backend === 's3' ? t('S3 (객체 저장소)') : t('로컬 저장소')}</span>
		</p>
		<p class="desc">
			<span class="muted">{t('전환 방향')}:</span> <span class="mono">{migrateDir}</span>
		</p>
		<div class="btn-row">
			<Button variant="outline" size="sm" disabled={busy || storageRunning} onclick={startMigration} aria-busy={storageRunning}>
				{#if storageRunning}<Spinner />{t('마이그레이션 중…')}{:else}{t('마이그레이션 시작')}{/if}
			</Button>
			{#if storageRunning}
				<span class="muted">
					{t('마이그레이션 중')} {storage.done ?? 0}/{storage.total ?? 0} ({migratePct})
					{#if (storage.failed_count ?? 0) > 0}<span class="warn">· {t('실패')} {storage.failed_count}</span>{/if}
					{#if storage.workers}· {t('동시')} {storage.workers}{/if}
					{#if migrateRate > 0}· ~{migrateRate.toFixed(1)} {t('파일/초')}{/if}
				</span>
			{/if}
		</div>

		{#if storage.status === 'partial' && storage.failed?.length}
			<p class="desc warn">{t('실패한 파일')} ({storage.failed.length})</p>
			<ul class="faillist mono">
				{#each storage.failed.slice(0, 50) as f}
					<li title={f.error}>{f.path}</li>
				{/each}
			</ul>
			<Button variant="outline" size="sm" class="self-start" disabled={busy || storageRunning} onclick={retryMigration}>{t('전체 재시도')}</Button>
		{/if}
	</fieldset>

	{#if storage.summary?.cleanup_pending}
		<fieldset class="sec danger">
			<legend>{t('원본 정리 대기')}</legend>
			<p class="desc">{t('마이그레이션이 완료되었습니다. 아래 원본을 수동으로 삭제한 뒤 정리 완료를 확인하세요 (원본은 자동 삭제되지 않습니다).')}</p>
			<p class="desc">
				<span class="muted">{t('전환 방향')}:</span>
				<span class="mono">{directionLabel(storage.summary.direction)}</span>
			</p>
			<p class="desc">
				<span class="muted">{t('원본 위치')}:</span>
				<span class="mono">{storage.summary.source_location ?? ''}</span>
			</p>
			<Button variant="outline" size="sm" class="self-start" disabled={busy || storageRunning} onclick={confirmCleanup}>{t('정리 완료 확인')}</Button>
		</fieldset>
	{/if}
{/if}

<fieldset class="sec">
	<legend>{t('DB 백업 (S3)')}</legend>
	<p class="desc">{t('index.db 와 rules.json 을 S3 의 db-backups/ 에 백업합니다 (S3 모드 전용). 전체 백업의 대체 내구성 수단입니다.')}</p>
	{#if !dbBackup}
		<p class="muted">{t('불러오는 중…')}</p>
	{:else if !dbBackup.s3_mode}
		<p class="muted">{t('S3 모드에서만 사용할 수 있습니다.')}</p>
	{:else}
		<p class="desc">
			<span class="muted">{t('마지막 백업')}:</span>
			<span class="mono">{dbBackup.last_at ?? t('없음')}</span>
			{#if dbBackup.last_status === 'error'}
				<span class="warn"> ({t('실패')}{dbBackup.last_error ? `: ${dbBackup.last_error}` : ''})</span>
			{/if}
		</p>
		<p class="desc">
			<span class="muted">{t('S3 백업 개수')}:</span> <span class="mono">{dbBackup.count}</span>
			{#if dbBackup.list_error}<span class="warn"> ({t('목록 조회 오류')}: {dbBackup.list_error})</span>{/if}
		</p>
		<div class="btn-row">
			<Button variant="outline" size="sm" disabled={busy} onclick={runDbBackup} aria-busy={pending === 'db-backup'}>
				{#if pending === 'db-backup'}<Spinner />{t('백업 중…')}{:else}{t('지금 백업')}{/if}
			</Button>
		</div>
		<label>{t('백업 주기(시간)')} <Input type="number" bind:value={dbbInterval} min="1" max="720" /></label>
		<label>{t('보존 개수')} <Input type="number" bind:value={dbbKeep} min="1" max="365" /></label>
		<Button variant="outline" size="sm" class="self-start" disabled={busy} onclick={saveDbBackupSettings}>{t('저장')}</Button>
	{/if}
</fieldset>

<!-- ── 아카이브 설정 ── -->
<h3 class="group">{t('아카이브 설정')}</h3>
<p class="desc">{t('아카이빙·크롤·문서 수집·로컬 네트워크 동작을 설정합니다.')}</p>

<fieldset class="sec">
	<legend>{t('사이트 아카이브 기본값')}</legend>
	<p class="desc">{t('사이트 전체 아카이브(크롤)의 기본 범위·간격입니다.')}</p>
	<label>{t('최대 페이지')} <Input type="number" bind:value={crawlMaxPages} /></label>
	<label>{t('최대 깊이')} <Input type="number" bind:value={crawlMaxDepth} /></label>
	<label>{t('지연(초)')} <Input type="number" bind:value={crawlDelay} /></label>
	<Button variant="outline" size="sm" class="self-start" disabled={busy} onclick={() => save('/system/crawl-settings', { crawl_max_pages: crawlMaxPages, crawl_max_depth: crawlMaxDepth, crawl_delay: crawlDelay })}>{t('저장')}</Button>
</fieldset>

<fieldset class="sec">
	<legend>{t('사이트 아카이브 최대값')}</legend>
	<p class="desc">{t('새 사이트 아카이브에 허용하는 상한과 실패 시 재시도 대기입니다. 기본값은 이 상한 이내로 조정됩니다.')}</p>
	<label>{t('최대 페이지')} <Input type="number" bind:value={limitMaxPages} /></label>
	<label>{t('최대 깊이')} <Input type="number" bind:value={limitMaxDepth} /></label>
	<label>{t('지연(초)')} <Input type="number" bind:value={limitMaxDelay} /></label>
	<label>{t('재시도 대기(초, 쉼표)')} <Input type="text" class="flex-1 basis-[180px] min-w-0" bind:value={crawlBackoff} /></label>
	<Button variant="outline" size="sm" class="self-start" disabled={busy} onclick={() => save('/system/crawl-limits', { crawl_max_pages: limitMaxPages, crawl_max_depth: limitMaxDepth, crawl_max_delay: limitMaxDelay, crawl_retry_backoff: crawlBackoff })}>{t('저장')}</Button>
</fieldset>

<fieldset class="sec">
	<legend>{t('캡처')}</legend>
	<p class="desc">{t('스냅샷을 찍을 때의 추가 캡처 동작입니다.')}</p>
	<label class="ck"><input type="checkbox" bind:checked={mobileShot} /> {t('모바일 스크린샷도 저장')}</label>
	<Button variant="outline" size="sm" class="self-start" disabled={busy} onclick={() => save('/system/capture-settings', { mobile_screenshot_enabled: mobileShot })}>{t('저장')}</Button>
</fieldset>

<fieldset class="sec">
	<legend>{t('AI 자동 챌린지 해결')}</legend>
	<p class="desc">{t('동의·연령 확인·"계속하려면 클릭" 같은 양성 게이트를 비전 LLM 이 스크린샷을 보고 마우스·키보드 입력으로 대신 통과시킵니다. 못 풀면 사람 보조(라이브)로 넘어갑니다. OpenAI 호환 API 를 직접 호출합니다.')}</p>
	{#if !s.ai_challenge_config.key_set}
		<AlertBox warn={t('WCCG_SECRET_KEY 가 설정되지 않아 API 키를 저장할 수 없습니다. 환경변수를 설정하세요.')} />
	{/if}
	<label class="ck"><input type="checkbox" bind:checked={aiEnabled} /> {t('사용')}</label>
	<label>{t('API 주소(base_url)')} <Input type="text" class="flex-1 basis-[180px] min-w-0" bind:value={aiBaseUrl} placeholder="https://api.openai.com/v1" /></label>
	<label>{t('모델')} <Input type="text" class="flex-1 basis-[180px] min-w-0" bind:value={aiModel} placeholder="gpt-4o" /></label>
	<label>{t('API 키')}
		<Input type="password" bind:value={aiApiKey}
			placeholder={s.ai_challenge_config.has_api_key ? t('설정됨') : ''} />
	</label>
	{#if s.ai_challenge_config.has_api_key}
		<label class="ck"><input type="checkbox" bind:checked={aiClearKey} /> {t('저장된 API 키 삭제')}</label>
	{/if}
	<label>{t('최대 라운드 수')} <Input type="number" bind:value={aiMaxRounds} min={s.ai_challenge_config.limits.max_rounds_min} max={s.ai_challenge_config.limits.max_rounds_max} /></label>
	<label>{t('판정 대기(ms)')} <Input type="number" bind:value={aiVerdictDelay} min={s.ai_challenge_config.limits.verdict_delay_ms_min} max={s.ai_challenge_config.limits.verdict_delay_ms_max} /></label>
	<label>{t('라운드당 액션 수 상한')} <Input type="number" bind:value={aiMaxActions} min={s.ai_challenge_config.limits.max_actions_min} max={s.ai_challenge_config.limits.max_actions_max} /></label>
	<label>{t('요청 타임아웃(초)')} <Input type="number" bind:value={aiRequestTimeout} min={s.ai_challenge_config.limits.request_timeout_min} max={s.ai_challenge_config.limits.request_timeout_max} /></label>
	<label class="ck"><input type="checkbox" bind:checked={aiSuccessRecheck} /> {t('통과 판정 교차확인(마커 잔존 시 계속)')}</label>
	<label class="full">{t('액션 프롬프트')}
		<Textarea bind:value={aiActionPrompt} rows={8} class="font-mono text-xs" />
	</label>
	<Button variant="outline" size="sm" class="self-start" onclick={resetAiActionPrompt}>{t('기본값으로 되돌리기')}</Button>
	<label class="full">{t('판정 프롬프트')}
		<Textarea bind:value={aiVerdictPrompt} rows={6} class="font-mono text-xs" />
	</label>
	<Button variant="outline" size="sm" class="self-start" onclick={resetAiVerdictPrompt}>{t('기본값으로 되돌리기')}</Button>
	<Button variant="outline" size="sm" class="self-start" disabled={busy} onclick={saveAiChallenge}>{t('저장')}</Button>
</fieldset>

<fieldset class="sec">
	<legend>{t('휴지통')}</legend>
	<p class="desc">{t('아카이브 삭제 시 즉시 지우지 않고 휴지통에 보관했다가 기간 경과 시 자동 삭제합니다. 끄면 삭제가 즉시 영구 삭제됩니다.')}</p>
	<label class="ck"><input type="checkbox" bind:checked={trashEnabled} /> {t('휴지통 사용')}</label>
	<label>{t('보관 기간(일, 0=자동삭제 끔)')} <Input type="number" bind:value={trashRetention} min={s.trash_retention_limits.min} max={s.trash_retention_limits.max} /></label>
	<Button variant="outline" size="sm" class="self-start" disabled={busy} onclick={() => save('/system/trash-settings', { trash_enabled: trashEnabled, trash_retention_days: trashRetention })}>{t('저장')}</Button>
</fieldset>

<fieldset class="sec">
	<legend>{t('확장 자격증명')}</legend>
	<p class="desc">{t('확장이 보낸 1회성 로그인 자격증명의 보관 시간입니다.')}</p>
	<label>{t('보관 시간(시간)')} <Input type="number" bind:value={credTtl} min={s.ext_credential_ttl_limits.min} max={s.ext_credential_ttl_limits.max} /></label>
	<Button variant="outline" size="sm" class="self-start" disabled={busy} onclick={() => save('/system/credential-settings', { ext_credential_ttl_hours: credTtl })}>{t('저장')}</Button>
</fieldset>

<fieldset class="sec">
	<legend>{t('문서 아카이브 한도')}</legend>
	<p class="desc">{t('페이지가 링크한 문서 파일을 받을 때의 한도입니다.')}</p>
	<label>{t('스냅샷당 수')} <Input type="number" bind:value={docCount} /></label>
	<label>{t('개당 크기(MB)')} <Input type="number" bind:value={docMb} /></label>
	<label>{t('다운로드 타임아웃(초)')} <Input type="number" bind:value={docTimeout} /></label>
	<Button variant="outline" size="sm" class="self-start" disabled={busy} onclick={() => save('/system/document-settings', { document_max_count: docCount, document_max_mb: docMb, document_fetch_timeout: docTimeout })}>{t('저장')}</Button>
</fieldset>

<fieldset class="sec">
	<legend>{t('로컬 네트워크 태그')}</legend>
	<p class="desc">{t('사설 IP(로컬 네트워크) 주소를 아카이빙할 때 붙이는 태그입니다.')}</p>
	<label>{t('이름')} <Input type="text" class="flex-1 basis-[180px] min-w-0" bind:value={newTagName} maxlength={60} /></label>
	<label>{t('설명')} <Input type="text" class="flex-1 basis-[180px] min-w-0" bind:value={newTagDesc} maxlength={200} /></label>
	<Button variant="outline" size="sm" class="self-start" disabled={busy || !newTagName.trim()} onclick={createTag}>{t('추가')}</Button>
</fieldset>
{#if s.network_tags.length === 0}
	<p class="muted">{t('등록된 태그가 없습니다.')}</p>
{:else}
	<ul class="taglist">
		{#each s.network_tags as tag}
			<li>
				<span class="mono">{tag.name}</span>
				<span class="muted mono">{tag.id}</span>
				<Button variant="outline" size="sm" class="ml-auto" disabled={busy} onclick={() => save(`/system/network-tags/${tag.id}/delete`)}>{t('삭제')}</Button>
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
			<Button variant="outline" size="sm" class="self-start" disabled={busy || !mergeSource || !mergeTarget} onclick={mergeTags}>{t('병합')}</Button>
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
	{#if signupEnabled && signupRole !== 'pending'}
		<p class="desc warn">{t('주의: 초기 권한이 승인 대기(pending)가 아니면 가입·SSO 자동 생성 계정이 관리자 승인 없이 곧바로 권한을 갖습니다.')}</p>
	{/if}
	<Button variant="outline" size="sm" class="self-start" disabled={busy} onclick={() => save('/system/settings', { signup_enabled: signupEnabled, signup_default_role: signupRole })}>{t('저장')}</Button>
</fieldset>
<fieldset class="sec">
	<legend>{t('이메일 본인 인증')}</legend>
	<p class="desc">{t('패스워드 계정이 로그인 전에 메일로 이메일을 검증하게 합니다.')}</p>
	<label class="ck"><input type="checkbox" bind:checked={evEnabled} /> {t('사용')}</label>
	<label>{t('코드 만료(분)')} <Input type="number" bind:value={evTtl} min={s.email_verification_ttl_limits.min} max={s.email_verification_ttl_limits.max} /></label>
	<Button variant="outline" size="sm" class="self-start" disabled={busy} onclick={() => save('/system/email-verification-settings', { email_verification_enabled: evEnabled, email_verification_ttl_minutes: evTtl })}>{t('저장')}</Button>
</fieldset>
<fieldset class="sec">
	<legend>{t('인증 보호 (무차별 대입 방어)')}</legend>
	<p class="desc">{t('로그인·2단계 인증·이메일 코드의 시도 횟수를 제한합니다. 한도를 넘으면 잠시 차단됩니다.')}</p>
	<label class="ck"><input type="checkbox" bind:checked={atEnabled} /> {t('사용')}</label>
	<label>{t('로그인 시도 한도(이메일별)')} <Input type="number" bind:value={atLoginLimit} min={s.auth_throttle_limits.limit_min} max={s.auth_throttle_limits.limit_max} /></label>
	<label>{t('로그인 시도 한도(IP별)')} <Input type="number" bind:value={atLoginIpLimit} min={s.auth_throttle_limits.limit_min} max={s.auth_throttle_limits.limit_max} /></label>
	<label>{t('로그인 카운트 창(분)')} <Input type="number" bind:value={atLoginWindow} min={s.auth_throttle_limits.window_min} max={s.auth_throttle_limits.window_max} /></label>
	<label>{t('2단계 인증 시도 한도')} <Input type="number" bind:value={atTotpLimit} min={s.auth_throttle_limits.limit_min} max={s.auth_throttle_limits.limit_max} /></label>
	<label>{t('이메일 코드 오답 한도')} <Input type="number" bind:value={atEmailVerifyLimit} min={s.auth_throttle_limits.limit_min} max={s.auth_throttle_limits.limit_max} /></label>
	<label>{t('이메일 코드 재발송 한도(시간당)')} <Input type="number" bind:value={atEmailResendLimit} min={s.auth_throttle_limits.limit_min} max={s.auth_throttle_limits.limit_max} /></label>
	<Button variant="outline" size="sm" class="self-start" disabled={busy} onclick={() => save('/system/auth-throttle-settings', { auth_throttle_enabled: atEnabled, login_limit: atLoginLimit, login_ip_limit: atLoginIpLimit, login_window_minutes: atLoginWindow, totp_limit: atTotpLimit, email_verify_limit: atEmailVerifyLimit, email_resend_limit: atEmailResendLimit })}>{t('저장')}</Button>
</fieldset>

<!-- ── 서버 환경설정 ── -->
<h3 class="group">{t('서버 환경설정')}</h3>
<p class="desc">{t('메일 발송과 API 키 등 서버 연동 설정입니다.')}</p>
<fieldset class="sec">
	<legend>{t('메일(SMTP)')} — {s.smtp_config.enabled ? t('사용 중') : t('미설정')}</legend>
	<p class="desc">{t('초대·이메일 인증 메일을 보내는 SMTP 서버입니다.')}</p>
	<label>{t('호스트')} <Input type="text" class="flex-1 basis-[180px] min-w-0" bind:value={smtpHost} /></label>
	<label>{t('포트')} <Input type="number" bind:value={smtpPort} min="1" max="65535" /></label>
	<label>{t('사용자')} <Input type="text" class="flex-1 basis-[180px] min-w-0" bind:value={smtpUser} /></label>
	<label>{t('보내는 주소')} <Input type="text" class="flex-1 basis-[180px] min-w-0" bind:value={smtpFrom} /></label>
	<label>TLS
		<select bind:value={smtpTls}>
			{#each s.smtp_tls_modes as m}<option value={m}>{m}</option>{/each}
		</select>
	</label>
	<label>{t('비밀번호')}
		<Input type="password" bind:value={smtpPassword}
			placeholder={s.smtp_config.has_password ? '••••••••' : ''} />
	</label>
	{#if s.smtp_config.has_password}
		<label class="ck"><input type="checkbox" bind:checked={smtpClearPw} /> {t('저장된 비밀번호 삭제')}</label>
	{/if}
	<div class="btn-row">
		<Button variant="outline" size="sm" disabled={busy} onclick={saveSmtp}>{t('저장')}</Button>
		<Button variant="outline" size="sm" disabled={busy || !s.smtp_config.enabled} onclick={testSmtp}>{t('테스트 메일 보내기')}</Button>
	</div>
</fieldset>
<p class="desc"><a href="{base}/system/api-keys">{t('API 키 관리로 이동')}</a></p>

<!-- ── 위험 구역 ── -->
<h3 class="group danger-title">{t('위험 구역')}</h3>
<p class="desc">{t('데이터 전체를 바꾸는 작업입니다 — 신중히 사용하세요.')}</p>
<fieldset class="sec danger">
	<legend>{t('데이터 관리')}</legend>
	<p class="desc">{t('전체 백업·복원과 아카이브 내보내기·가져오기입니다.')}</p>
	{#if isS3}
		<p class="desc warn">{t('S3 모드에서는 전체 백업·복원이 비활성화됩니다 (blob 이 로컬에 없음). 내구성은 S3 DB 백업으로, 데이터 이동은 내보내기/가져오기로 하세요.')}</p>
	{/if}
	<div class="btn-row">
		<Button variant="outline" size="sm" disabled={busy || isS3} onclick={() => doDownload('/system/backup')} aria-busy={pending === '/system/backup'}>
			{#if pending === '/system/backup'}<Spinner />{t('백업 준비중…')}{:else}{t('전체 백업 다운로드')}{/if}
		</Button>
		<Button variant="outline" size="sm" disabled={busy} onclick={() => doDownload('/system/export')} aria-busy={pending === '/system/export'}>
			{#if pending === '/system/export'}<Spinner />{t('내보내기 준비중…')}{:else}{t('아카이브 내보내기')}{/if}
		</Button>
	</div>
	<label>{t('백업 복원')}
		<input type="file" accept=".ccg.backup" disabled={busy || isS3}
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
			onchange={(e) =>
				uploadFile(
					e,
					'/system/import',
					importMode === 'overwrite'
						? t('덮어쓰기 모드로 가져오면 겹치는 아카이브가 현재 데이터를 덮어씁니다. 진행하시겠습니까?')
						: t('선택한 파일의 아카이브를 가져옵니다. 진행하시겠습니까?'),
					{ mode: importMode }
				)} />
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
			<Button variant="outline" size="sm" disabled={busy} onclick={() => migrationAction('regenerate')}>{t('토큰 재발급')}</Button>
			<Button variant="outline" size="sm" disabled={busy} onclick={() => migrationAction('disable')}>{t('이전 모드 끄기')}</Button>
		</div>
	{:else}
		<Button variant="outline" size="sm" class="self-start" disabled={busy} onclick={() => migrationAction('enable')}>{t('이전 모드 켜기')}</Button>
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
	.desc.warn {
		color: var(--warn, #b4690e);
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
		max-width: 720px;
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
	.sec label.full {
		flex-direction: column;
		align-items: stretch;
		justify-content: flex-start;
	}
	.sec label select {
		flex: 1 1 180px;
		min-width: 0;
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
	.btn-row {
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
	}
	.mtoken {
		background: var(--code-bg, var(--border));
		border-radius: 4px;
		padding: 8px 10px;
		font-size: 12px;
		word-break: break-all;
	}
	/* 마이그레이션 실패 파일 목록 — 길면 스크롤 */
	.faillist {
		list-style: none;
		padding: 6px 10px;
		margin: 0;
		max-width: 560px;
		max-height: 160px;
		overflow-y: auto;
		background: var(--bg-soft);
		border-radius: 4px;
		font-size: 12px;
	}
	.faillist li {
		padding: 2px 0;
		word-break: break-all;
	}
</style>
