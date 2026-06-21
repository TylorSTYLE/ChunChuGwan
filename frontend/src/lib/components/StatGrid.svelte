<script lang="ts">
	import type { Snippet } from 'svelte';
	/** 통계 카드 그리드 — auto-fit 반응형(모바일에서 더 촘촘히). */
	let { children }: { children: Snippet } = $props();
</script>

<div
	class="mb-2 grid grid-cols-[repeat(auto-fit,minmax(160px,1fr))] gap-2.5 max-[599px]:grid-cols-[repeat(auto-fit,minmax(130px,1fr))]"
>
	{@render children()}
</div>
