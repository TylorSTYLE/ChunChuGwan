<script lang="ts">
	import type { Snippet } from 'svelte';
	/** 폼 섹션 — 상단 구분선 + 대문자 섹션 헤더 + 본문 슬롯. */
	let {
		title,
		children
	}: { title: string; children: Snippet } = $props();
</script>

<section class="form-section">
	<h3 class="section-title">{title}</h3>
	<div class="section-body">{@render children()}</div>
</section>

<style>
	.form-section {
		border-top: 1px solid var(--border);
		padding-top: 16px;
		margin-top: 18px;
	}
	.section-title {
		font-size: 11px;
		font-weight: 600;
		color: var(--muted);
		text-transform: uppercase;
		letter-spacing: 0.04em;
		margin: 0 0 12px;
	}
	.section-body {
		display: flex;
		flex-direction: column;
		gap: 14px;
	}
</style>
