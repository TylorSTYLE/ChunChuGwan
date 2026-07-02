<script lang="ts">
	import { untrack } from 'svelte';
	import { base } from '$app/paths';
	import { goto } from '$app/navigation';
	import { t } from '$lib/i18n';
	import { api, ApiError } from '$lib/api';
	import AlertBox from '$lib/components/AlertBox.svelte';
	import FormSection from '$lib/components/FormSection.svelte';
	import Field from '$lib/components/Field.svelte';
	import Segmented from '$lib/components/Segmented.svelte';
	import Toggle from '$lib/components/Toggle.svelte';
	import ChipGroup from '$lib/components/ChipGroup.svelte';
	import HostBadge from '$lib/components/HostBadge.svelte';
	import { Button } from '$lib/components/ui/button';
	import { Input } from '$lib/components/ui/input';
	import { Textarea } from '$lib/components/ui/textarea';

	type Tag = { id: string; name: string; description: string | null };
	type Cred = { id: number; label: string; kind: string; kind_label: string };
	type CrawlDefaults = { max_pages: number; max_depth: number; delay: number };
	type CrawlLimits = { max_pages: number; max_depth: number; max_delay: number };
	let {
		data
	}: {
		data: {
			networkTags: Tag[];
			canManageCred: boolean;
			crawlDefaults: CrawlDefaults | null;
			crawlLimits: CrawlLimits | null;
		};
	} = $props();
	const tags = $derived(data.networkTags);
	const canManageCred = $derived(data.canManageCred);

	let url = $state('');
	let force = $state(false);
	// 클러스터 공유 허용 — 기본 꺼짐(=보내기 불가/보호). payload 의 protect 는 !shareCluster.
	let shareCluster = $state(false);
	// 캡처 범위 세그먼트 — 'single'=단일 페이지, 'site'=사이트 전체. payload 의 site 는 파생값.
	let scope = $state<'single' | 'site'>('single');
	const site = $derived(scope === 'site');
	let interval = $state('0');
	// 사이트 아카이브 옵션은 시스템 설정의 기본값으로 채운다 (비우면 서버가 같은 기본값 적용).
	let maxPages = $state(untrack(() => (data.crawlDefaults ? String(data.crawlDefaults.max_pages) : '')));
	let maxDepth = $state(untrack(() => (data.crawlDefaults ? String(data.crawlDefaults.max_depth) : '')));
	let delay = $state(untrack(() => (data.crawlDefaults ? String(data.crawlDefaults.delay) : '')));
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
		try {
			const r = await api<{
				credentials: Cred[];
				kinds: { value: string; label: string }[];
				secret_key_configured: boolean;
			}>(`/archive/credentials?url=${encodeURIComponent(u)}`);
			// 응답이 늦게 도착하는 사이 URL 이 바뀌었으면 이 결과는 버린다(다른 도메인의
			// 자격증명이 남는 것을 방지). credLoadedFor 는 성공 시에만 설정해 실패한 URL 은
			// 재조회(blur)가 막히지 않게 한다.
			if (u !== url.trim()) return;
			credLoadedFor = u;
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
	const host = $derived(hostOf(url));
	const netKind = $derived(classify(host));
	const isLoopback = $derived(netKind === 'loopback');

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

	// 캡처 범위 세그먼트 옵션 (리터럴 t() — i18n 정적 검사 대상).
	const scopeOptions = $derived([
		{ value: 'single', label: t('단일 페이지') },
		{ value: 'site', label: t('사이트 전체') }
	]);

	// 하단 요약 — host · 범위 · 주기.
	const scopeLabel = $derived(site ? t('사이트 전체') : t('단일 페이지'));
	const intervalLabel = $derived(
		interval === '0' ? t('1회') : t(INTERVALS.find(([v]) => v === interval)?.[1] ?? '없음')
	);

	// type="number" 입력을 Svelte 는 number(빈 값은 null)로 바인딩하지만, /archive 는
	// 폼 스타일 all-string 모델(ArchiveReq)이라 문자열로 보낸다 (''=서버가 시스템 기본값으로 해석).
	const numStr = (v: number | string | null): string => (v == null ? '' : String(v));

	async function submit(e: Event) {
		e.preventDefault();
		if (!url.trim() || isLoopback) return;
		busy = true;
		error = '';
		try {
			const r = await api<{ site: boolean; crawl_id?: number }>('/archive', {
				method: 'POST',
				body: JSON.stringify({
					url: url.trim(),
					force,
					protect: !shareCluster,
					site,
					interval,
					network_tag: networkTag,
					crawl_max_pages: numStr(maxPages),
					crawl_max_depth: numStr(maxDepth),
					crawl_delay: numStr(delay),
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

<AlertBox {error} />

<form onsubmit={submit} class="archive-form">
	<Field label="URL" hint={t('아카이빙할 페이지의 전체 주소를 입력하세요.')}>
		<Input type="url" bind:value={url} placeholder="https://example.com" required onchange={loadCreds} />
	</Field>
	{#if url.trim()}<HostBadge kind={netKind} />{/if}

	{#if netKind === 'private'}
		<Field label={t('로컬 네트워크 태그')}>
			{#if tags.length > 0}
				<select bind:value={networkTag}>
					<option value="">{t('선택 안 함 (공개 주소)')}</option>
					{#each tags as tag}
						<option value={tag.id}>{tag.name}{tag.description ? ` — ${tag.description}` : ''}</option>
					{/each}
				</select>
			{/if}
		</Field>
		<p class="muted net-hint">
			{t('입력한 주소가 사설 IP 대역(로컬 네트워크)입니다 — 태그를 선택해야 아카이빙할 수 있습니다.')}
			{#if tags.length === 0}
				<a href="{base}/system/general"
					>{t('등록된 로컬 네트워크 태그가 없습니다 — 시스템 화면에서 먼저 추가하세요.')}</a
				>
			{/if}
		</p>
	{/if}

	<FormSection title={t('캡처 범위')}>
		<Segmented bind:value={scope} options={scopeOptions} />
		{#if site}
			<div class="crawl-opts">
				<Field label={t('최대 페이지')}><Input class="w-full min-w-0" type="number" bind:value={maxPages} min="1" max={data.crawlLimits?.max_pages} /></Field>
				<Field label={t('최대 깊이')}><Input class="w-full min-w-0" type="number" bind:value={maxDepth} min="0" max={data.crawlLimits?.max_depth} /></Field>
				<Field label={t('지연(초)')}><Input class="w-full min-w-0" type="number" bind:value={delay} min="0" max={data.crawlLimits?.max_delay} /></Field>
			</div>
			<p class="muted hint">
				{t('같은 호스트의 경로 프리픽스 이하를 모두 따라가 저장합니다. 비우면 시스템 기본값이 적용됩니다.')}
			</p>
		{:else}
			<p class="muted hint">{t('입력한 URL 한 페이지만 스냅샷으로 저장합니다.')}</p>
		{/if}
		<Toggle
			bind:checked={force}
			label={t('콘텐츠 동일해도 강제 저장')}
			description={t('기본값은 본문 해시가 바뀐 경우에만 새 스냅샷을 만듭니다.')}
		/>
		<Toggle
			bind:checked={shareCluster}
			label={t('다른 클러스터로 공유 허용')}
			description={t('기본은 보호(전송 안 함)입니다. 켜면 연결된 클러스터로 이 아카이브를 보낼 수 있습니다.')}
		/>
	</FormSection>

	<FormSection title={t('자동 재아카이빙')}>
		<ChipGroup bind:value={interval} options={INTERVALS} />
	</FormSection>

	<FormSection title={t('로그인 자격증명')}>
		{#if canManageCred}
			<select bind:value={credExisting}>
				<option value="">{t('연결 안 함')}</option>
				{#each existingCreds as c}
					<option value={String(c.id)}>{c.label} ({c.kind_label})</option>
				{/each}
				<option value="__new__">{t('새 자격증명 추가…')}</option>
			</select>
			{#if credExisting === '__new__'}
				<div class="cred-new">
					{#if !secretKeyConfigured}
						<div class="error">{t('WCCG_SECRET_KEY 가 설정되지 않아 자격증명을 저장할 수 없습니다.')}</div>
					{/if}
					<Field label={t('종류')}>
						<select bind:value={credKind}>
							{#each credKinds as k}<option value={k.value}>{k.label}</option>{/each}
						</select>
					</Field>
					<Field label={t('이름')}><Input type="text" bind:value={credLabel} maxlength={50} /></Field>
					{#if credKind === 'http_basic'}
						<Field label={t('사용자명')}><Input type="text" bind:value={credUsername} autocomplete="off" /></Field>
						<Field label={t('비밀번호')}>
							<Input type="password" bind:value={credPassword} autocomplete="new-password" />
						</Field>
					{:else if credKind === 'session'}
						<Field
							label={t('세션 상태 (storage_state JSON)')}
							hint={t('HAR 파일 업로드는 사이트 상세 화면에서 지원합니다.')}
						>
							<Textarea bind:value={credStorageState} rows={5} spellcheck="false" />
						</Field>
					{:else if credKind === 'jwt'}
						<Field label={t('Bearer 토큰')}>
							<Textarea bind:value={credToken} rows={3} spellcheck="false" autocomplete="off" />
						</Field>
					{/if}
				</div>
			{/if}
			<p class="muted creds-note">
				{t('이 도메인에 등록된 자격증명을 연결하거나 새로 추가할 수 있습니다. 아카이빙 시 로그인에 사용됩니다.')}
			</p>
		{:else}
			<p class="muted creds-note">
				{t('로그인이 필요한 사이트의 자격증명 연결은 사이트 상세 화면에서 관리합니다.')}
			</p>
		{/if}
	</FormSection>

	<div class="foot">
		<span class="summary">{host || '—'} · {scopeLabel} · {intervalLabel}</span>
		<Button type="submit" disabled={busy || isLoopback}>
			{busy ? t('등록 중…') : t('아카이빙 등록')}
		</Button>
	</div>
</form>

<style>
	.archive-form {
		max-width: 640px;
		display: flex;
		flex-direction: column;
		gap: 14px;
	}
	.crawl-opts {
		display: grid;
		grid-template-columns: repeat(3, minmax(0, 1fr));
		gap: 10px;
		padding: 12px;
		background: var(--bg-soft);
		border-radius: 6px;
	}
	.cred-new {
		display: flex;
		flex-direction: column;
		gap: 10px;
		padding: 12px;
		background: var(--bg-soft);
		border: 1px solid var(--border);
		border-radius: 6px;
	}
	.hint,
	.net-hint,
	.creds-note {
		font-size: 12px;
		margin: 0;
	}
	.foot {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 14px;
		border-top: 1px solid var(--border);
		margin-top: 18px;
		padding-top: 15px;
	}
	.summary {
		font-size: 12px;
		color: var(--muted);
		overflow-wrap: anywhere;
	}
</style>
