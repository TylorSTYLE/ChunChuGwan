<script lang="ts">
	import type { Snippet } from 'svelte';
	/** 폼 필드 래퍼 — 대문자 라벨 + 컨트롤 슬롯 + 힌트(세로 스택).
	 * 슬롯 컨트롤(input·select·textarea)은 stretch 되어 너비를 채운다. */
	let {
		label,
		hint = '',
		children
	}: { label: string; hint?: string; children: Snippet } = $props();
</script>

<div class="flex flex-col gap-1.5">
	<span class="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">{label}</span>
	{@render children()}
	{#if hint}<span class="text-xs text-muted-foreground">{hint}</span>{/if}
</div>
