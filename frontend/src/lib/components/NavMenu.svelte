<script lang="ts">
	/** 헤더 상단 메뉴의 드롭다운 그룹 (아카이브·로그·설정).
	 *
	 * 기존 user-menu 와 같은 `<details>` + absolute 패널 패턴을 재사용한다.
	 * SPA 클라이언트 이동은 전체 새로고침이 없어 `<details open>` 상태가 남으므로,
	 * 항목 클릭 시 닫도록 children 스니펫에 `close` 콜백을 넘긴다.
	 * 좁은 화면(<1024px)에서는 absolute 패널 대신 인라인 아코디언으로 펼쳐진다. */
	import type { Snippet } from 'svelte';

	let {
		label,
		badge = 0,
		children
	}: { label: string; badge?: number; children: Snippet<[() => void]> } = $props();

	let open = $state(false);
	function close() {
		open = false;
	}
</script>

<details class="nav-menu" name="hdrmenu" bind:open>
	<summary>
		{label}
		{#if badge > 0}<span class="nh-badge">{badge}</span>{/if}
	</summary>
	<div class="nav-menu-items">
		{@render children(close)}
	</div>
</details>

<style>
	.nav-menu {
		position: relative;
		font-size: 12px;
	}
	.nav-menu summary {
		cursor: pointer;
		list-style: none;
		padding: 4px 2px;
		color: var(--fg);
		white-space: nowrap;
	}
	.nav-menu summary::-webkit-details-marker {
		display: none;
	}
	.nav-menu summary::after {
		content: ' ▾';
		color: var(--muted);
		font-size: 10px;
	}
	.nav-menu summary:hover {
		color: var(--link);
	}
	.nav-menu-items {
		position: absolute;
		left: 0;
		top: 100%;
		margin-top: 4px;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 4px;
		padding: 4px;
		display: flex;
		flex-direction: column;
		min-width: 180px;
		z-index: 20;
		box-shadow: 0 4px 12px rgba(0, 0, 0, 0.08);
	}
	/* 항목은 layout 에서 일반 <a> 로 들어온다 — 전역이 아닌 :global 로 패딩만 입힌다. */
	.nav-menu-items :global(a) {
		font-size: 13px;
		padding: 7px 8px;
		color: var(--fg);
		text-decoration: none;
		border-radius: 3px;
		white-space: nowrap;
	}
	.nav-menu-items :global(a:hover) {
		background: var(--bg-soft);
		text-decoration: none;
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

	/* 좁은 화면: absolute 드롭다운 대신 인라인 아코디언 (헤더 ☰ 안에서 펼쳐짐). */
	@media (max-width: 1023px) {
		.nav-menu {
			width: 100%;
		}
		.nav-menu summary {
			padding: 7px 4px;
			font-size: 13px;
			border-top: 1px solid var(--border);
		}
		.nav-menu-items {
			position: static;
			margin-top: 0;
			border: none;
			box-shadow: none;
			padding: 0 0 4px 12px;
			min-width: 0;
		}
	}
</style>
