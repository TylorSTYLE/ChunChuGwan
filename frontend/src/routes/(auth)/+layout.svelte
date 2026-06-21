<script lang="ts">
	import { t } from '$lib/i18n';
	import { userPrefersMode, setMode } from 'mode-watcher';
	import type { Snippet } from 'svelte';

	let { children }: { children: Snippet } = $props();

	// 테마 토글 — 미인증 화면도 자동 → 라이트 → 다크 순환(루트 헤더가 없으므로 자체 제공).
	// 실제 적용·저장은 루트 레이아웃의 mode-watcher(ModeWatcher)가 담당한다.
	const NEXT: Record<string, 'light' | 'dark' | 'system'> = {
		system: 'light',
		light: 'dark',
		dark: 'system'
	};
	function cycleTheme() {
		setMode(NEXT[userPrefersMode.current]);
	}
</script>

<header class="auth">
	<button type="button" onclick={cycleTheme} title={t('테마 전환 (자동 → 라이트 → 다크)')}>◐</button>
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

<style>
	header.auth {
		display: flex;
		justify-content: flex-end;
		align-items: center;
		padding: 8px 16px;
	}
	header.auth button {
		font: inherit;
		font-size: 14px;
		padding: 3px 10px;
		border: 1px solid var(--border);
		border-radius: 4px;
		background: var(--surface);
		color: var(--fg);
		cursor: pointer;
	}
	header.auth button:hover {
		background: var(--bg-soft);
	}
</style>
