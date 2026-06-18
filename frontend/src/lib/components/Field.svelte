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

<div class="field">
	<span class="field-label">{label}</span>
	{@render children()}
	{#if hint}<span class="field-hint">{hint}</span>{/if}
</div>

<style>
	.field {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	.field-label {
		font-size: 11px;
		font-weight: 600;
		color: var(--muted);
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}
	.field-hint {
		font-size: 12px;
		color: var(--muted);
	}
</style>
