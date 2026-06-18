<script lang="ts">
	import type { Snippet } from 'svelte';
	/** 통계 카드 그리드(전역 .stat-grid — auto-fit, 반응형). */
	let { children }: { children: Snippet } = $props();
</script>

<div class="stat-grid">{@render children()}</div>
