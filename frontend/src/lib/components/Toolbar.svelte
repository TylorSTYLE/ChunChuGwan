<script lang="ts">
	import type { Snippet } from 'svelte';
	/** 필터/검색 바 — 좁은 화면에서 줄바꿈(전역 .toolbar). 사이 여백은 .spacer 로. */
	let { children }: { children: Snippet } = $props();
</script>

<div class="toolbar">{@render children()}</div>
