<script lang="ts">
	import '../app.css';
	import { base } from '$app/paths';
	import { goto } from '$app/navigation';
	import { t } from '$lib/i18n';
	import { ModeWatcher, userPrefersMode, setMode } from 'mode-watcher';
	import NavMenu from '$lib/components/NavMenu.svelte';
	import NavProgress from '$lib/components/NavProgress.svelte';
	import UpdateNoticeModal from '$lib/components/UpdateNoticeModal.svelte';
	import { Button, buttonVariants } from '$lib/components/ui/button';
	import { Toaster } from '$lib/components/ui/sonner';
	import * as DropdownMenu from '$lib/components/ui/dropdown-menu';
	import * as Popover from '$lib/components/ui/popover';
	import * as Sheet from '$lib/components/ui/sheet';
	import Search from '@lucide/svelte/icons/search';
	import Puzzle from '@lucide/svelte/icons/puzzle';
	import Sun from '@lucide/svelte/icons/sun';
	import Moon from '@lucide/svelte/icons/moon';
	import Monitor from '@lucide/svelte/icons/monitor';
	import Menu from '@lucide/svelte/icons/menu';
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

	// 헤더 네비 구조 — 데스크탑 드롭다운과 모바일 시트가 같은 데이터를 공유한다.
	// 라벨은 t() 리터럴로 둬 i18n 정적 검사가 en 카탈로그를 강제하게 한다.
	type NavItem = { href: string; label: string; badge?: number };
	type NavGroup = { label: string; badge: number; items: NavItem[] };
	const menuGroups = $derived.by<NavGroup[]>(() => {
		if (!me) return [];
		const groups: NavGroup[] = [];

		const arch: NavItem[] = [];
		if (me.flags.can_archive) arch.push({ href: '/archive/new', label: t('새 아카이빙') });
		arch.push({ href: '/archive/list', label: t('아카이브 사이트 목록') });
		arch.push({ href: '/archive/documents', label: t('전체 문서(파일)') });
		arch.push({ href: '/archive/schedules', label: t('스케줄') });
		if (me.flags.can_manage_trash) arch.push({ href: '/archive/trash', label: t('휴지통') });
		if (me.flags.can_manage_system && me.needs_human_count > 0)
			arch.push({
				href: '/archive/needs-human',
				label: t('사람 확인'),
				badge: me.needs_human_count
			});
		groups.push({
			label: t('아카이브'),
			badge: me.flags.can_manage_system ? me.needs_human_count : 0,
			items: arch
		});

		if (showLogs) {
			const logs: NavItem[] = [];
			if (me.flags.can_view_archive_logs)
				logs.push({ href: '/log/archive', label: t('아카이브 로그') });
			if (me.flags.can_view_system_logs)
				logs.push({ href: '/log/system', label: t('시스템 로그') });
			if (me.flags.can_view_audit_logs) logs.push({ href: '/log/audit', label: t('감사 로그') });
			groups.push({ label: t('로그'), badge: 0, items: logs });
		}

		if (showSettings) {
			const sys: NavItem[] = [];
			if (me.flags.can_manage_users) sys.push({ href: '/system/users', label: t('사용자 관리') });
			if (me.flags.can_manage_system) sys.push({ href: '/system/groups', label: t('권한 그룹') });
			if (me.flags.can_manage_users)
				sys.push({ href: '/system/api-keys', label: t('API Key 관리') });
			if (me.flags.can_manage_system)
				sys.push({ href: '/system/general', label: t('시스템 설정') });
			groups.push({ label: t('설정'), badge: 0, items: sys });
		}

		return groups;
	});

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

	let navOpen = $state(false);
	let extOpen = $state(false);

	// 전역 헤더 검색창 — 제출 시 기존 /search 전용 페이지로 이동한다.
	let q = $state('');
	function search(e: Event) {
		e.preventDefault();
		const term = q.trim();
		navOpen = false;
		goto(`${base}/search${term ? `?q=${encodeURIComponent(term)}` : ''}`);
	}

	// 로그아웃 — 숨은 POST 폼을 프로그래밍으로 제출한다(서버가 세션 종료 후 리다이렉트).
	let logoutForm: HTMLFormElement;
</script>

<ModeWatcher />
<Toaster />
<NavProgress />
<form method="POST" action="/logout" bind:this={logoutForm} class="hidden"></form>

{#if me && me.user?.role !== 'pending'}
	<header
		class="sticky top-0 z-30 flex flex-wrap items-center gap-x-3.5 gap-y-2 border-b bg-background/95 px-4 py-2 backdrop-blur supports-[backdrop-filter]:bg-background/80 max-[599px]:px-3"
	>
		<h1 class="m-0 text-[15px] font-semibold">
			<a href="{base}/" class="text-foreground no-underline">{t('춘추관')}</a>
		</h1>
		<span class="text-xs text-muted-foreground max-[599px]:hidden">{t('개인 웹 아카이브')}</span>

		<!-- 데스크탑 네비 — 드롭다운 그룹들 -->
		<nav class="hidden items-center gap-1 lg:flex">
			{#each menuGroups as g (g.label)}
				<NavMenu label={g.label} badge={g.badge}>
					{#snippet children(close)}
						{#each g.items as it (it.href)}
							<a
								href="{base}{it.href}"
								onclick={close}
								class={it.badge ? 'flex items-center justify-between gap-2' : ''}
							>
								{it.label}
								{#if it.badge}
									<span
										class="rounded-lg bg-changed-bg px-1.5 text-[11px] font-semibold text-changed"
										>{it.badge}</span
									>
								{/if}
							</a>
						{/each}
					{/snippet}
				</NavMenu>
			{/each}
		</nav>

		{#if me.flags.can_search}
			<form
				class="order-10 flex basis-full items-center gap-2 rounded-full border bg-card px-3 py-1 focus-within:border-link lg:order-none lg:max-w-[520px] lg:flex-1 lg:basis-auto"
				role="search"
				onsubmit={search}
			>
				<Search class="size-4 shrink-0 text-muted-foreground" />
				<input
					type="search"
					bind:value={q}
					class="min-w-0 flex-1 border-none bg-transparent p-0 text-[13px] outline-none placeholder:text-muted-foreground"
					placeholder={t('아카이브 본문·문서에서 검색…')}
					aria-label={t('검색')}
				/>
			</form>
		{/if}

		<div class="ml-auto flex items-center gap-1.5">
			<!-- 크롬 확장 안내 팝오버 -->
			<Popover.Root bind:open={extOpen}>
				<Popover.Trigger
					class={buttonVariants({ variant: 'ghost', size: 'icon' })}
					title={t('크롬 확장')}
					aria-label={t('크롬 확장')}
				>
					<Puzzle class="size-5" />
				</Popover.Trigger>
				<Popover.Content align="end" class="w-[300px] max-w-[86vw] text-sm">
					<h4 class="mb-1.5 flex items-baseline gap-2 text-[13px] font-semibold">
						{t('크롬 확장')}
						<span class="font-mono text-xs text-muted-foreground">v{me.version}</span>
					</h4>
					<p class="mb-2 text-xs leading-relaxed text-muted-foreground">
						{t(
							'크롬 확장을 설치하면 보고 있는 페이지를 클릭 한 번으로 아카이브하고, 아카이브 히스토리도 바로 확인할 수 있습니다.'
						)}
					</p>
					<a
						class={buttonVariants({ variant: 'outline', size: 'sm' })}
						href="/extension/download"
						onclick={() => (extOpen = false)}>{t('크롬 확장 내려받기')}</a
					>
					<details class="mt-3">
						<summary class="cursor-pointer text-xs text-link">{t('설치 방법')}</summary>
						<ol class="mt-2 list-decimal pl-[18px] text-xs leading-relaxed text-muted-foreground">
							<li>{t('내려받은 ZIP 파일의 압축을 풉니다.')}</li>
							<li>
								{t('크롬 주소창에')} <span class="font-mono">chrome://extensions</span>
								{t('를 엽니다.')}
							</li>
							<li>{t('우측 상단 ‘개발자 모드’를 켭니다.')}</li>
							<li>{t('‘압축해제된 확장 프로그램을 로드’를 눌러 압축 푼 폴더를 선택합니다.')}</li>
							<li>
								{t(
									'확장 아이콘을 눌러 이 춘추관 주소와, 개인 API Key 화면에서 발급한 키를 입력하면 연결됩니다.'
								)}
							</li>
						</ol>
					</details>
				</Popover.Content>
			</Popover.Root>

			<Button
				variant="ghost"
				size="icon"
				onclick={cycleTheme}
				title={t(THEME_LABELS[userPrefersMode.current])}
				aria-label={t(THEME_LABELS[userPrefersMode.current])}
			>
				{#if userPrefersMode.current === 'light'}
					<Sun class="size-4" />
				{:else if userPrefersMode.current === 'dark'}
					<Moon class="size-4" />
				{:else}
					<Monitor class="size-4" />
				{/if}
			</Button>

			{#if me.user}
				<DropdownMenu.Root>
					<DropdownMenu.Trigger
						class="max-w-[180px] truncate font-mono text-xs text-muted-foreground outline-none hover:text-foreground max-[599px]:max-w-[110px]"
					>
						{me.user.display_name || me.user.email}
					</DropdownMenu.Trigger>
					<DropdownMenu.Content align="end" class="min-w-[160px]">
						<DropdownMenu.Item onSelect={() => goto(`${base}/settings/account`)}>
							{t('계정')}
						</DropdownMenu.Item>
						{#if me.flags.can_use_api_keys}
							<DropdownMenu.Item onSelect={() => goto(`${base}/settings/api-keys`)}>
								{t('개인 API Key')}
							</DropdownMenu.Item>
						{/if}
						<DropdownMenu.Item onSelect={() => goto(`${base}/settings/archives`)}>
							{t('내 아카이브')}
						</DropdownMenu.Item>
						<DropdownMenu.Separator />
						<DropdownMenu.Item variant="destructive" onSelect={() => logoutForm.requestSubmit()}>
							{t('로그아웃')}
						</DropdownMenu.Item>
					</DropdownMenu.Content>
				</DropdownMenu.Root>
			{/if}

			<!-- 모바일 네비 — 시트(좌측 드로어) -->
			<Sheet.Root bind:open={navOpen}>
				<Sheet.Trigger
					class="{buttonVariants({ variant: 'ghost', size: 'icon' })} lg:hidden"
					title={t('메뉴')}
					aria-label={t('메뉴')}
				>
					<Menu class="size-5" />
				</Sheet.Trigger>
				<Sheet.Content side="left" class="w-[280px] overflow-y-auto">
					<Sheet.Header>
						<Sheet.Title>{t('춘추관')}</Sheet.Title>
					</Sheet.Header>
					<nav class="flex flex-col gap-4 px-4 pb-4">
						{#each menuGroups as g (g.label)}
							<div>
								<div
									class="mb-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground"
								>
									{g.label}
								</div>
								<div class="flex flex-col">
									{#each g.items as it (it.href)}
										<a
											href="{base}{it.href}"
											onclick={() => (navOpen = false)}
											class="flex items-center justify-between gap-2 rounded-sm px-2 py-1.5 text-sm text-foreground no-underline hover:bg-muted"
										>
											{it.label}
											{#if it.badge}
												<span
													class="rounded-lg bg-changed-bg px-1.5 text-[11px] font-semibold text-changed"
													>{it.badge}</span
												>
											{/if}
										</a>
									{/each}
								</div>
							</div>
						{/each}
					</nav>
				</Sheet.Content>
			</Sheet.Root>
		</div>
	</header>
{/if}

{#if chrome && showUpdate && updateNote}
	<UpdateNoticeModal note={updateNote} onclose={dismissUpdate} />
{/if}

<main class="mx-auto max-w-[1280px] p-4 max-[599px]:p-3" class:!max-w-none={!chrome} class:!p-0={!chrome}>
	{@render children()}
</main>
