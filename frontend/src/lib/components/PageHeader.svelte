<script lang="ts">
	import type { Snippet } from 'svelte';
	/** 페이지 헤더 — h2 제목 + 선택적 우측 액션 슬롯(모바일에서 줄바꿈). */
	let { title, actions }: { title: string; actions?: Snippet } = $props();
</script>

<div class="mb-3 flex flex-wrap items-baseline justify-between gap-2.5">
	<h2 class="text-base font-semibold">{title}</h2>
	{#if actions}<div class="flex flex-wrap items-center gap-2">{@render actions()}</div>{/if}
</div>
