<script lang="ts">
	/** 헤더 상단 메뉴의 드롭다운 그룹 (아카이브·로그·설정) — shadcn DropdownMenu 기반.
	 *
	 * children 스니펫에 `close` 콜백을 넘긴다 — 항목(<a>)은 클릭 시 close 로 메뉴를 닫는다
	 * (SPA 이동은 새로고침이 없어 열린 상태가 남기 때문). badge>0 이면 라벨 옆에 숫자 배지. */
	import type { Snippet } from 'svelte';
	import * as DropdownMenu from '$lib/components/ui/dropdown-menu';
	import ChevronDown from '@lucide/svelte/icons/chevron-down';

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

<DropdownMenu.Root bind:open>
	<DropdownMenu.Trigger
		class="inline-flex items-center gap-1 whitespace-nowrap px-0.5 py-1 text-xs text-foreground outline-none hover:text-link data-[state=open]:text-link"
	>
		{label}
		{#if badge > 0}
			<span class="ml-1 rounded-lg bg-changed-bg px-1.5 text-[11px] font-semibold text-changed">
				{badge}
			</span>
		{/if}
		<ChevronDown class="size-2.5 text-muted-foreground" />
	</DropdownMenu.Trigger>
	<DropdownMenu.Content
		align="start"
		class="min-w-[180px] [&_a]:block [&_a]:rounded-sm [&_a]:px-2 [&_a]:py-1.5 [&_a]:text-[13px] [&_a]:text-foreground [&_a]:no-underline [&_a:hover]:bg-muted"
	>
		{@render children(close)}
	</DropdownMenu.Content>
</DropdownMenu.Root>
