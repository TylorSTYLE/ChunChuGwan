<script lang="ts">
	import '../app.css';
	import { base } from '$app/paths';
	import { t } from '$lib/i18n';
	import type { Snippet } from 'svelte';
	import type { Me } from '$lib/types';

	let { data, children }: { data: { me: Me }; children: Snippet } = $props();
	const me = $derived(data.me);
	const flags = $derived(me.flags);

	// 테마 토글 — 자동(시스템) → 라이트 → 다크 순환 (base.html 의 wccgTheme 재사용).
	const THEME_LABELS: Record<string, string> = {
		auto: '테마: 자동',
		light: '테마: 라이트',
		dark: '테마: 다크'
	};
	const NEXT: Record<string, string> = { auto: 'light', light: 'dark', dark: 'auto' };
	let themeMode = $state(getMode());

	function getMode(): string {
		if (typeof window === 'undefined') return 'auto';
		return window.wccgTheme?.stored() ?? 'auto';
	}
	function cycleTheme() {
		const next = NEXT[getMode()];
		try {
			if (next === 'auto') localStorage.removeItem(window.wccgTheme.KEY);
			else localStorage.setItem(window.wccgTheme.KEY, next);
		} catch {
			/* localStorage 불가 — 시스템 설정 유지 */
		}
		window.wccgTheme.apply();
		themeMode = getMode();
	}

	// 좁은 화면 메뉴 토글
	let navOpen = $state(false);
</script>

<header>
	<h1><a href="{base}/">{t('춘추관')}</a></h1>
	<span class="muted tagline">{t('개인 웹 아카이브')}</span>
	<button
		type="button"
		id="nav-toggle"
		aria-expanded={navOpen}
		onclick={() => (navOpen = !navOpen)}
		title={t('메뉴')}>☰</button
	>
	<nav class:open={navOpen}>
		<a href="{base}/">{t('현황')}</a>
		<a href="{base}/archives">{t('아카이브 사이트 목록')}</a>
		<a href="{base}/documents">{t('전체 문서(파일)')}</a>
		{#if flags.can_search}<a href="{base}/search">{t('검색')}</a>{/if}
		{#if flags.can_archive}<a href="{base}/archive/new">{t('새 아카이빙')}</a>{/if}
		<a href="{base}/schedules">{t('스케줄')}</a>
		{#if flags.can_view_logs}<a href="{base}/logs">{t('아카이빙 로그')}</a>{/if}
		{#if flags.can_manage_users}<a href="{base}/system/users">{t('사용자')}</a>{/if}
		{#if flags.can_manage_system}<a href="{base}/system/groups">{t('권한 그룹')}</a>{/if}
		{#if flags.can_manage_users}<a href="{base}/system/api-keys">{t('API 키')}</a>{/if}
		{#if flags.can_manage_system}<a href="{base}/system">{t('시스템')}</a>{/if}
		{#if flags.can_manage_system}<a href="{base}/system/logs">{t('시스템 로그')}</a>{/if}
		<span class="spacer"></span>
		<button type="button" onclick={cycleTheme}>{THEME_LABELS[themeMode]}</button>
		{#if me.user}
			<span class="mono muted nav-user">{me.user.display_name || me.user.email}</span>
		{/if}
	</nav>
</header>

<main>
	{@render children()}
</main>

<style>
	header {
		border-bottom: 1px solid var(--border);
		padding: 8px 16px;
		display: flex;
		gap: 16px;
		align-items: baseline;
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
	header .tagline,
	header nav a {
		font-size: 12px;
	}
	header nav {
		display: contents;
	}
	header nav .spacer {
		flex: 1;
	}
	.nav-user {
		font-size: 12px;
	}
	#nav-toggle {
		display: none;
	}
	@media (max-width: 1023px) {
		header {
			align-items: center;
		}
		#nav-toggle {
			display: inline-block;
			margin-left: auto;
		}
		header nav {
			display: none;
			width: 100%;
			flex-direction: column;
			align-items: stretch;
			gap: 2px;
			padding: 8px 0 4px;
		}
		header nav.open {
			display: flex;
		}
		header nav a {
			font-size: 13px;
			padding: 7px 4px;
		}
		header nav .spacer {
			flex: none;
		}
	}
	@media (max-width: 599px) {
		header .tagline {
			display: none;
		}
	}
</style>
