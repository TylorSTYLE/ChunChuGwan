<script lang="ts">
	import { base } from '$app/paths';
	import { goto } from '$app/navigation';
	import { t } from '$lib/i18n';
	import { api, ApiError } from '$lib/api';

	type Tag = { id: string; name: string; description: string | null };
	type Cred = { id: number; label: string; kind: string; kind_label: string };
	let { data }: { data: { networkTags: Tag[]; canManageCred: boolean } } = $props();
	const tags = $derived(data.networkTags);
	const canManageCred = $derived(data.canManageCred);

	let url = $state('');
	let force = $state(false);
	let site = $state(false);
	let interval = $state('0');
	let maxPages = $state('');
	let maxDepth = $state('');
	let delay = $state('');
	let networkTag = $state('');
	let error = $state('');
	let busy = $state(false);

	// 로그인 자격증명 연결 (자격증명 관리 권한) — URL 도메인의 기존 자격증명 조회 + 신규 생성
	let credExisting = $state(''); // ''=연결 안 함, '__new__'=신규, 숫자=기존 id
	let credKind = $state('http_basic');
	let credLabel = $state('');
	let credUsername = $state('');
	let credPassword = $state('');
	let credStorageState = $state('');
	let credToken = $state('');
	let existingCreds = $state<Cred[]>([]);
	let credKinds = $state<{ value: string; label: string }[]>([]);
	let secretKeyConfigured = $state(true);
	let credLoadedFor = $state('');

	async function loadCreds() {
		const u = url.trim();
		if (!canManageCred || u === credLoadedFor) return;
		credLoadedFor = u;
		try {
			const r = await api<{
				credentials: Cred[];
				kinds: { value: string; label: string }[];
				secret_key_configured: boolean;
			}>(`/archive/credentials?url=${encodeURIComponent(u)}`);
			existingCreds = r.credentials;
			credKinds = r.kinds;
			secretKeyConfigured = r.secret_key_configured;
			if (credExisting && credExisting !== '__new__' && !r.credentials.some((c) => String(c.id) === credExisting))
				credExisting = '';
		} catch {
			existingCreds = [];
		}
	}

	// 입력 URL 의 호스트가 사설 IP 대역이면 태그 선택을 띄우고, 루프백이면 거부 안내.
	// IP 리터럴만 판정한다 — 사설 호스트명(DNS)은 서버 게이트가 제출 시 잡는다.
	function hostOf(u: string): string {
		let v = (u || '').trim();
		if (!v) return '';
		if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(v)) v = 'http://' + v;
		try {
			return new URL(v).hostname.replace(/^\[|\]$/g, '').toLowerCase();
		} catch {
			return '';
		}
	}
	function classify(h: string): 'public' | 'private' | 'loopback' {
		if (!h) return 'public';
		if (h === 'localhost' || h === '::1' || /^127\./.test(h)) return 'loopback';
		if (
			/^10\./.test(h) ||
			/^192\.168\./.test(h) ||
			/^172\.(1[6-9]|2[0-9]|3[01])\./.test(h) ||
			/^169\.254\./.test(h) ||
			/^(fc|fd)[0-9a-f]{2}:/.test(h) ||
			/^fe80:/.test(h)
		)
			return 'private';
		return 'public';
	}
	const netKind = $derived(classify(hostOf(url)));

	// 주기 선택지 — app._SCHEDULE_OPTIONS 와 동일(초 단위). "0" = 없음.
	const INTERVALS: [string, string][] = [
		['0', '없음'],
		['3600', '1시간'],
		['10800', '3시간'],
		['21600', '6시간'],
		['43200', '12시간'],
		['86400', '1일'],
		['259200', '3일'],
		['604800', '1주일'],
		['2592000', '1개월']
	];

	async function submit(e: Event) {
		e.preventDefault();
		if (!url.trim()) return;
		busy = true;
		error = '';
		try {
			const r = await api<{ site: boolean; crawl_id?: number }>('/archive', {
				method: 'POST',
				body: JSON.stringify({
					url: url.trim(),
					force,
					site,
					interval,
					network_tag: networkTag,
					crawl_max_pages: maxPages,
					crawl_max_depth: maxDepth,
					crawl_delay: delay,
					cred_existing_id: credExisting,
					cred_kind: credKind,
					cred_label: credLabel,
					cred_username: credUsername,
					cred_password: credPassword,
					cred_storage_state: credStorageState,
					cred_token: credToken
				})
			});
			// 단발은 목록으로, 사이트 전체는 사이트 목록으로 (크롤 진행 화면은 후속)
			goto(`${base}/archive/list`);
			void r;
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
			busy = false;
		}
	}
</script>

<h2>{t('새 아카이빙')}</h2>

{#if error}<div class="error">{error}</div>{/if}

<form onsubmit={submit} class="archive-form">
	<label>
		URL
		<input type="url" bind:value={url} placeholder="https://example.com" required onchange={loadCreds} />
	</label>

	<label class="check"><input type="checkbox" bind:checked={force} /> {t('콘텐츠 동일해도 강제 저장')}</label>
	<label class="check"><input type="checkbox" bind:checked={site} /> {t('사이트 전체 아카이브 (같은 호스트)')}</label>

	{#if site}
		<div class="crawl-opts">
			<label>{t('최대 페이지')}<input type="number" bind:value={maxPages} min="1" /></label>
			<label>{t('최대 깊이')}<input type="number" bind:value={maxDepth} min="0" /></label>
			<label>{t('지연(초)')}<input type="number" bind:value={delay} min="0" /></label>
		</div>
	{/if}

	{#if netKind === 'loopback'}
		<div class="error">{t('루프백 주소는 아카이빙할 수 없습니다.')}</div>
	{:else if netKind === 'private'}
		<label>
			{t('로컬 네트워크 태그')}
			{#if tags.length > 0}
				<select bind:value={networkTag}>
					<option value="">{t('선택 안 함 (공개 주소)')}</option>
					{#each tags as tag}
						<option value={tag.id}>{tag.name}{tag.description ? ` — ${tag.description}` : ''}</option>
					{/each}
				</select>
			{/if}
		</label>
		<p class="muted net-hint">
			{t('입력한 주소가 사설 IP 대역(로컬 네트워크)입니다 — 태그를 선택해야 아카이빙할 수 있습니다.')}
			{#if tags.length === 0}
				<a href="{base}/system/general"
					>{t('등록된 로컬 네트워크 태그가 없습니다 — 시스템 화면에서 먼저 추가하세요.')}</a
				>
			{/if}
		</p>
	{/if}

	<label>
		{t('자동 재아카이빙 주기')}
		<select bind:value={interval}>
			{#each INTERVALS as [v, label]}<option value={v}>{t(label)}</option>{/each}
		</select>
	</label>

	{#if canManageCred}
		<label>
			{t('로그인 자격증명')}
			<select bind:value={credExisting}>
				<option value="">{t('연결 안 함')}</option>
				{#each existingCreds as c}
					<option value={String(c.id)}>{c.label} ({c.kind_label})</option>
				{/each}
				<option value="__new__">{t('새 자격증명 추가…')}</option>
			</select>
		</label>
		{#if credExisting === '__new__'}
			<div class="cred-new">
				{#if !secretKeyConfigured}
					<div class="error">{t('WCCG_SECRET_KEY 가 설정되지 않아 자격증명을 저장할 수 없습니다.')}</div>
				{/if}
				<label>{t('종류')}
					<select bind:value={credKind}>
						{#each credKinds as k}<option value={k.value}>{k.label}</option>{/each}
					</select>
				</label>
				<label>{t('이름')} <input type="text" bind:value={credLabel} maxlength="50" /></label>
				{#if credKind === 'http_basic'}
					<label>{t('사용자명')} <input type="text" bind:value={credUsername} autocomplete="off" /></label>
					<label>{t('비밀번호')} <input type="password" bind:value={credPassword} autocomplete="new-password" /></label>
				{:else if credKind === 'session'}
					<label>{t('세션 상태 (storage_state JSON)')}
						<textarea bind:value={credStorageState} rows="5" spellcheck="false"></textarea>
					</label>
					<p class="muted hint">{t('HAR 파일 업로드는 사이트 상세 화면에서 지원합니다.')}</p>
				{:else if credKind === 'jwt'}
					<label>{t('Bearer 토큰')}
						<textarea bind:value={credToken} rows="3" spellcheck="false" autocomplete="off"></textarea>
					</label>
				{/if}
			</div>
		{/if}
		<p class="muted" style="font-size:12px">
			{t('이 도메인에 등록된 자격증명을 연결하거나 새로 추가할 수 있습니다. 아카이빙 시 로그인에 사용됩니다.')}
		</p>
	{:else}
		<p class="muted" style="font-size:12px">
			{t('로그인이 필요한 사이트의 자격증명 연결은 사이트 상세 화면에서 관리합니다.')}
		</p>
	{/if}

	<button type="submit" class="primary" disabled={busy}>
		{busy ? t('등록 중…') : t('아카이빙 등록')}
	</button>
</form>

<style>
	.archive-form {
		max-width: 560px;
		display: flex;
		flex-direction: column;
		gap: 12px;
	}
	.archive-form label {
		display: flex;
		flex-direction: column;
		gap: 4px;
		font-size: 13px;
	}
	.archive-form label.check {
		flex-direction: row;
		align-items: center;
		gap: 6px;
	}
	.crawl-opts {
		display: flex;
		gap: 10px;
	}
	.crawl-opts label {
		flex: 1;
	}
	.error {
		background: var(--red-bg);
		color: var(--red-text);
		border-radius: 4px;
		padding: 8px 12px;
		margin-bottom: 12px;
		font-size: 13px;
	}
	button.primary {
		align-self: flex-start;
		color: #fff;
		background: #16a34a;
		border-color: #16a34a;
		padding: 6px 18px;
	}
	button.primary:hover {
		background: #15803d;
	}
	button.primary:disabled {
		opacity: 0.6;
	}
</style>
