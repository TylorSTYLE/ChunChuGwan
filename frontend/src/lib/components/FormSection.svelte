<script lang="ts">
	import type { Snippet } from 'svelte';
	/** 폼 섹션 — 상단 구분선 + 대문자 섹션 헤더 + 본문 슬롯. */
	let { title, children }: { title: string; children: Snippet } = $props();
</script>

<section class="mt-[18px] border-t pt-4">
	<h3 class="mb-3 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
		{title}
	</h3>
	<div class="flex flex-col gap-3.5">{@render children()}</div>
</section>
