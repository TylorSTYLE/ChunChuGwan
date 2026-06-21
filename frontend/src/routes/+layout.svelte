<script lang="ts">
	import '../app.css';
	import { base } from '$app/paths';
	import { goto } from '$app/navigation';
	import { t } from '$lib/i18n';
	import { ModeWatcher, userPrefersMode, setMode } from 'mode-watcher';
	import NavMenu from '$lib/components/NavMenu.svelte';
	import UpdateNoticeModal from '$lib/components/UpdateNoticeModal.svelte';
	import type { Snippet } from 'svelte';
	import type { Me } from '$lib/types';

	let { data, children }: { data: { me: Me | null }; children: Snippet } = $props();
	// me 가 null(미인증)이거나 승인 대기(pending)면 헤더 없이 셸만 렌더한다 —
	// 미인증은 (auth) 로그인·setup, pending 은 안내 화면이 children 으로 들어온다.
	const me = $derived(data.me);
	const chrome = $derived(me && me.user?.role !== 'pending');

	// 업데이트 안내 — 로그인 후 현재 버전 노트를 1회 모달로 띄운다. "봤음"은
	// localStorage(본 버전 문자열)로만 추적해 같은 버전은 다시 뜨지 않는다(브라우저
	// 기준). 새 버전이면 다시 뜬다. 백엔드(/me)가 현재 버전 노트가 없으면 null 을 준다.
	const UPDATE_SEEN_KEY = 'wccg-seen-update';
	function seenUpdateVersion(): string | null {
		if (typeof window === 'undefined') return null;
		try {
			return localStorage.getItem(UPDATE_SEEN_KEY);
		} catch {
			return null;
		}
	}
	let seenUpdate = $state(seenUpdateVersion());
	const updateNote = $derived(me?.release_note ?? null);
	const showUpdate = $derived(!!updateNote && seenUpdate !== updateNote.version);
	function dismissUpdate() {
		const v = updateNote?.version;
		if (!v) return;
		try {
			localStorage.setItem(UPDATE_SEEN_KEY, v);
		} catch {
			/* localStorage 불가 — 이 탭에서만 닫힘 유지 */
		}
		seenUpdate = v; // localStorage 실패해도 이 탭에선 즉시 닫힘
	}

	// 로그·설정 그룹은 하위에 보일 항목이 하나라도 있을 때만 노출한다.
	const showLogs = $derived(!!me && me.flags.can_view_any_logs);
	const showSettings = $derived(!!me && (me.flags.can_manage_users || me.flags.can_manage_system));

	// 테마 토글 — 자동(시스템) → 라이트 → 다크 순환 (mode-watcher). .dark 클래스·
	// localStorage 저장·FOUC 방지는 mode-watcher(ModeWatcher)가 담당한다.
	const THEME_LABELS: Record<string, string> = {
		system: '테마: 자동',
		light: '테마: 라이트',
		dark: '테마: 다크'
	};
	const NEXT: Record<string, 'light' | 'dark' | 'system'> = {
		system: 'light',
		light: 'dark',
		dark: 'system'
	};
	function cycleTheme() {
		setMode(NEXT[userPrefersMode.current]);
	}

	// 좁은 화면 메뉴 토글
	let navOpen = $state(false);
	// 개인설정·확장 드롭다운 — SPA 클라이언트 이동은 전체 새로고침이 없어 <details open>
	// 상태가 남으므로, 항목 클릭 시 직접 닫는다.
	let userMenuOpen = $state(false);
	let extOpen = $state(false);

	// 전역 헤더 검색창 — 제출 시 기존 /search 전용 페이지로 이동한다.
	let q = $state('');
	function search(e: Event) {
		e.preventDefault();
		const term = q.trim();
		navOpen = false;
		goto(`${base}/search${term ? `?q=${encodeURIComponent(term)}` : ''}`);
	}

	// 헤더 드롭다운(<details>)은 그룹 name="hdrmenu" 으로 한 번에 하나만 열린다(네이티브
	// 배타). 메뉴 밖을 클릭하면 열려 있던 드롭다운을 닫는다 — 네이티브 details 는
	// 외부 클릭으로 닫히지 않기 때문(메뉴 내부 클릭·항목 선택은 각자 처리).
	$effect(() => {
		function onDocClick(e: MouseEvent) {
			const target = e.target as Element | null;
			if (target?.closest('header details')) return;
			document
				.querySelectorAll<HTMLDetailsElement>('header details[open]')
				.forEach((d) => (d.open = false));
		}
		document.addEventListener('click', onDocClick);
		return () => document.removeEventListener('click', onDocClick);
	});
</script>

<ModeWatcher />

{#if me && me.user?.role !== 'pending'}
<header>
	<h1><a href="{base}/">{t('춘추관')}</a></h1>
	<span class="muted tagline">{t('개인 웹 아카이브')}</span>

	<nav class:open={navOpen}>
		<NavMenu label={t('아카이브')} badge={me.flags.can_manage_system ? me.needs_human_count : 0}>
			{#snippet children(close)}
				{#if me.flags.can_archive}
					<a href="{base}/archive/new" onclick={close}>{t('새 아카이빙')}</a>
				{/if}
				<a href="{base}/archive/list" onclick={close}>{t('아카이브 사이트 목록')}</a>
				<a href="{base}/archive/documents" onclick={close}>{t('전체 문서(파일)')}</a>
				<a href="{base}/archive/schedules" onclick={close}>{t('스케줄')}</a>
					{#if me.flags.can_manage_trash}
						<a href="{base}/archive/trash" onclick={close}>{t('휴지통')}</a>
					{/if}
				{#if me.flags.can_manage_system && me.needs_human_count > 0}
					<a href="{base}/archive/needs-human" class="needs-human" onclick={close}>
						{t('사람 확인')}<span class="nh-badge">{me.needs_human_count}</span>
					</a>
				{/if}
			{/snippet}
		</NavMenu>

		{#if showLogs}
			<NavMenu label={t('로그')}>
				{#snippet children(close)}
					{#if me.flags.can_view_archive_logs}
						<a href="{base}/log/archive" onclick={close}>{t('아카이브 로그')}</a>
					{/if}
					{#if me.flags.can_view_system_logs}
						<a href="{base}/log/system" onclick={close}>{t('시스템 로그')}</a>
					{/if}
					{#if me.flags.can_view_audit_logs}
						<a href="{base}/log/audit" onclick={close}>{t('감사 로그')}</a>
					{/if}
				{/snippet}
			</NavMenu>
		{/if}

		{#if showSettings}
			<NavMenu label={t('설정')}>
				{#snippet children(close)}
					{#if me.flags.can_manage_users}
						<a href="{base}/system/users" onclick={close}>{t('사용자 관리')}</a>
					{/if}
					{#if me.flags.can_manage_system}
						<a href="{base}/system/groups" onclick={close}>{t('권한 그룹')}</a>
					{/if}
					{#if me.flags.can_manage_users}
						<a href="{base}/system/api-keys" onclick={close}>{t('API Key 관리')}</a>
					{/if}
					{#if me.flags.can_manage_system}
						<a href="{base}/system/general" onclick={close}>{t('시스템 설정')}</a>
					{/if}
				{/snippet}
			</NavMenu>
		{/if}
	</nav>

	{#if me.flags.can_search}
		<form class="hdr-search" role="search" onsubmit={search}>
			<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"
				><path
					d="M21 21l-4.3-4.3M11 18a7 7 0 110-14 7 7 0 010 14z"
					fill="none"
					stroke="currentColor"
					stroke-width="2"
					stroke-linecap="round"
				/></svg
			>
			<input
				type="search"
				bind:value={q}
				placeholder={t('아카이브 본문·문서에서 검색…')}
				aria-label={t('검색')}
			/>
		</form>
	{/if}

	<div class="hdr-actions">
		<!-- 크롬 확장 안내 팝오버 -->
		<details class="ext-menu" name="hdrmenu" bind:open={extOpen}>
			<summary title={t('크롬 확장')} aria-label={t('크롬 확장')}>
				<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"
					><path
						d="M12 3a3 3 0 013 3h3a2 2 0 012 2v3a3 3 0 010 6v3a2 2 0 01-2 2h-3a3 3 0 00-6 0H6a2 2 0 01-2-2v-3a3 3 0 010-6V8a2 2 0 012-2h3a3 3 0 013-3z"
						fill="none"
						stroke="currentColor"
						stroke-width="1.6"
						stroke-linejoin="round"
					/></svg
				>
			</summary>
			<div class="ext-panel">
				<h4>{t('크롬 확장')} <span class="mono muted">v{me.version}</span></h4>
				<p class="muted">
					{t(
						'크롬 확장을 설치하면 보고 있는 페이지를 클릭 한 번으로 아카이브하고, 아카이브 히스토리도 바로 확인할 수 있습니다.'
					)}
				</p>
				<p>
					<a class="ext-dl" href="/extension/download" onclick={() => (extOpen = false)}
						>{t('크롬 확장 내려받기')}</a
					>
				</p>
				<details class="ext-steps">
					<summary>{t('설치 방법')}</summary>
					<ol class="muted">
						<li>{t('내려받은 ZIP 파일의 압축을 풉니다.')}</li>
						<li>
							{t('크롬 주소창에')} <span class="mono">chrome://extensions</span>
							{t('를 엽니다.')}
						</li>
						<li>{t('우측 상단 ‘개발자 모드’를 켭니다.')}</li>
						<li>{t('‘압축해제된 확장 프로그램을 로드’를 눌러 압축 푼 폴더를 선택합니다.')}</li>
						<li>{t('확장 아이콘을 눌러 이 춘추관 주소와, 개인 API Key 화면에서 발급한 키를 입력하면 연결됩니다.')}</li>
					</ol>
				</details>
			</div>
		</details>

		<button type="button" class="theme-btn" onclick={cycleTheme}>{t(THEME_LABELS[userPrefersMode.current])}</button>

		{#if me.user}
			<details class="user-menu" name="hdrmenu" bind:open={userMenuOpen}>
				<summary class="mono muted">{me.user.display_name || me.user.email}</summary>
				<div class="user-menu-items">
					<a href="{base}/settings/account" onclick={() => (userMenuOpen = false)}>{t('계정')}</a>
					{#if me.flags.can_use_api_keys}
						<a href="{base}/settings/api-keys" onclick={() => (userMenuOpen = false)}
							>{t('개인 API Key')}</a
						>
					{/if}
					<a href="{base}/settings/archives" onclick={() => (userMenuOpen = false)}
						>{t('내 아카이브')}</a
					>
					<form method="POST" action="/logout"><button type="submit">{t('로그아웃')}</button></form>
				</div>
			</details>
		{/if}

		<button
			type="button"
			id="nav-toggle"
			aria-expanded={navOpen}
			onclick={() => (navOpen = !navOpen)}
			title={t('메뉴')}>☰</button
		>
	</div>
</header>
{/if}

{#if chrome && showUpdate && updateNote}
	<UpdateNoticeModal note={updateNote} onclose={dismissUpdate} />
{/if}

<main class:plain={!chrome}>
	{@render children()}
</main>

<style>
	header {
		border-bottom: 1px solid var(--border);
		padding: 8px 16px;
		display: flex;
		gap: 8px 14px;
		align-items: center;
		flex-wrap: wrap;
	}
	header h1 {
		font-size: 15px;
		margin: 0;
	}
	header h1 a {
		color: var(--fg);
		text-decoration: none;
	}
	header .tagline {
		font-size: 12px;
	}
	/* nav 는 데스크탑에서 박스가 아니라 그룹 버튼들이 헤더 flex 에 직접 참여하게 둔다. */
	header nav {
		display: contents;
	}

	/* 전역 검색창 — 구글 스타일 둥근 입력. 데스크탑에서 가운데 공간을 흡수한다. */
	.hdr-search {
		display: flex;
		align-items: center;
		gap: 6px;
		flex: 1 1 220px;
		max-width: 520px;
		padding: 4px 12px;
		border: 1px solid var(--border);
		border-radius: 999px;
		background: var(--surface);
	}
	.hdr-search:focus-within {
		border-color: var(--link);
	}
	.hdr-search .icon {
		width: 16px;
		height: 16px;
		color: var(--muted);
		flex: none;
	}
	.hdr-search input {
		flex: 1;
		min-width: 0;
		border: none;
		background: none;
		padding: 2px 0;
		font-size: 13px;
		color: var(--fg);
	}
	.hdr-search input:focus {
		outline: none;
	}

	.hdr-actions {
		display: flex;
		align-items: center;
		gap: 10px;
		margin-left: auto;
	}
	.theme-btn {
		font-size: 12px;
		white-space: nowrap;
	}

	.needs-human {
		color: var(--amber);
	}
	.nh-badge {
		display: inline-block;
		margin-left: 4px;
		padding: 0 6px;
		border-radius: 8px;
		background: var(--amber-bg);
		color: var(--amber);
		font-size: 11px;
		font-weight: 600;
	}

	/* 확장 팝오버 — user-menu 와 같은 <details> 패턴 */
	.ext-menu {
		position: relative;
		display: flex;
	}
	.ext-menu summary {
		cursor: pointer;
		list-style: none;
		display: inline-flex;
		color: var(--muted);
	}
	.ext-menu summary::-webkit-details-marker {
		display: none;
	}
	.ext-menu summary:hover {
		color: var(--link);
	}
	.ext-menu .icon {
		width: 20px;
		height: 20px;
	}
	.ext-panel {
		position: absolute;
		right: 0;
		top: 100%;
		margin-top: 6px;
		width: 300px;
		max-width: 86vw;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: 12px 14px;
		z-index: 30;
		box-shadow: 0 6px 18px rgba(0, 0, 0, 0.12);
	}
	.ext-panel h4 {
		margin: 0 0 6px;
		font-size: 13px;
	}
	.ext-panel p {
		margin: 0 0 8px;
		font-size: 12px;
		line-height: 1.5;
	}
	.ext-dl {
		display: inline-block;
		padding: 5px 12px;
		border: 1px solid var(--border);
		border-radius: 4px;
		font-size: 13px;
	}
	.ext-dl:hover {
		background: var(--bg-soft);
		text-decoration: none;
	}
	.ext-steps summary {
		cursor: pointer;
		font-size: 12px;
		color: var(--link);
	}
	.ext-steps ol {
		margin: 8px 0 0;
		padding-left: 18px;
		font-size: 12px;
		line-height: 1.6;
	}

	/* 개인설정 드롭다운 */
	.user-menu {
		position: relative;
		font-size: 12px;
	}
	.user-menu summary {
		cursor: pointer;
		list-style: none;
		max-width: 180px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.user-menu summary::-webkit-details-marker {
		display: none;
	}
	.user-menu-items {
		position: absolute;
		right: 0;
		top: 100%;
		margin-top: 4px;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 4px;
		padding: 4px;
		display: flex;
		flex-direction: column;
		min-width: 150px;
		z-index: 20;
		box-shadow: 0 4px 12px rgba(0, 0, 0, 0.08);
	}
	.user-menu-items a,
	.user-menu-items button {
		font-size: 13px;
		padding: 6px 8px;
		text-align: left;
		background: none;
		border: none;
		color: var(--fg);
		text-decoration: none;
		cursor: pointer;
		width: 100%;
		border-radius: 3px;
	}
	.user-menu-items a:hover,
	.user-menu-items button:hover {
		background: var(--bg-soft);
	}
	.user-menu-items form {
		margin: 0;
	}

	#nav-toggle {
		display: none;
		font-size: 16px;
		line-height: 1;
		padding: 4px 9px;
	}

	/* ── 태블릿·모바일: nav 그룹은 ☰ 안으로, 검색창은 풀폭 줄로 내린다 ── */
	@media (max-width: 1023px) {
		#nav-toggle {
			display: inline-flex;
		}
		/* 검색창과 nav 를 헤더의 다음 줄(풀폭)로 내린다 (order > 0). */
		.hdr-search {
			order: 5;
			flex: 1 1 100%;
			max-width: none;
		}
		header nav {
			order: 6;
			display: none;
			flex: 1 1 100%;
			flex-direction: column;
			align-items: stretch;
			gap: 0;
		}
		header nav.open {
			display: flex;
		}
	}
	@media (max-width: 599px) {
		header {
			padding: 8px 12px;
		}
		header .tagline {
			display: none;
		}
		.hdr-actions {
			gap: 8px;
		}
		.theme-btn {
			font-size: 11px;
		}
		.user-menu summary {
			max-width: 110px;
		}
	}
</style>
