<script lang="ts">
	import type { Snippet } from 'svelte';
	/** 일반 카드 — 테두리 박스 + 선택적 대문자 제목 + 본문 슬롯. */
	let { title = '', children }: { title?: string; children: Snippet } = $props();
</script>

<section class="rounded-lg border p-3.5">
	{#if title}
		<h3 class="mb-2.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
			{title}
		</h3>
	{/if}
	<div class="flex flex-col gap-3">{@render children()}</div>
</section>
