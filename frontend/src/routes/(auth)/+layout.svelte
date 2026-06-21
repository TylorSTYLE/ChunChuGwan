<script lang="ts">
	import { t } from '$lib/i18n';
	import { userPrefersMode, setMode } from 'mode-watcher';
	import { Button } from '$lib/components/ui/button';
	import Sun from '@lucide/svelte/icons/sun';
	import Moon from '@lucide/svelte/icons/moon';
	import Monitor from '@lucide/svelte/icons/monitor';
	import type { Snippet } from 'svelte';

	let { children }: { children: Snippet } = $props();

	// 테마 토글 — 미인증 화면도 자동 → 라이트 → 다크 순환(루트 헤더가 없으므로 자체 제공).
	// 실제 적용·저장은 루트 레이아웃의 mode-watcher(ModeWatcher)가 담당한다.
	const LABELS: Record<string, string> = {
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
</script>

<header class="flex items-center justify-end px-4 py-2">
	<Button
		variant="ghost"
		size="icon"
		onclick={cycleTheme}
		title={t(LABELS[userPrefersMode.current])}
		aria-label={t(LABELS[userPrefersMode.current])}
	>
		{#if userPrefersMode.current === 'light'}
			<Sun class="size-4" />
		{:else if userPrefersMode.current === 'dark'}
			<Moon class="size-4" />
		{:else}
			<Monitor class="size-4" />
		{/if}
	</Button>
</header>

<div class="auth-shell">
	<div class="auth-brand">
		<svg
			class="logo"
			width="60"
			height="60"
			viewBox="0 0 64 64"
			aria-hidden="true"
			xmlns="http://www.w3.org/2000/svg"
		>
			<path
				fill="currentColor"
				d="M32 6Q16 20 2 24L7 29Q20 25 32 15Q44 25 57 29L62 24Q48 20 32 6Z"
			/>
			<rect fill="currentColor" x="14" y="34" width="36" height="6" rx="1.5" />
			<rect fill="currentColor" x="14" y="43" width="36" height="6" rx="1.5" />
			<rect fill="currentColor" x="14" y="52" width="26" height="6" rx="1.5" />
			<rect fill="var(--seal, #c2410c)" x="44" y="52" width="6" height="6" rx="1" />
		</svg>
		<div class="auth-title">{t('춘추관')}</div>
		<div class="auth-tagline">{t('개인 웹 아카이브')}</div>
	</div>
	{@render children()}
</div>
