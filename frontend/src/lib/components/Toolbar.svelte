<script lang="ts">
	import type { Snippet } from 'svelte';
	/** 필터/검색 바 — 좁은 화면에서 줄바꿈. 사이 여백은 자식 .spacer 로 채운다. */
	let { children }: { children: Snippet } = $props();
</script>

<div class="mb-3 flex flex-wrap items-center gap-2 [&_.spacer]:flex-1">{@render children()}</div>
