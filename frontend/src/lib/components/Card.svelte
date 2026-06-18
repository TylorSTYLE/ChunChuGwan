<script lang="ts">
	import type { Snippet } from 'svelte';
	/** 일반 카드 — 테두리 박스 + 선택적 대문자 제목 + 본문 슬롯. */
	let { title = '', children }: { title?: string; children: Snippet } = $props();
</script>

<section class="card">
	{#if title}<h3 class="card-title">{title}</h3>{/if}
	<div class="card-body">{@render children()}</div>
</section>

<style>
	.card {
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: 12px 14px;
	}
	.card-title {
		margin: 0 0 10px;
		font-size: 11px;
		font-weight: 600;
		color: var(--muted);
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}
	.card-body {
		display: flex;
		flex-direction: column;
		gap: 12px;
	}
</style>
