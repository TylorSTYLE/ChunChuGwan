<script lang="ts">
	import type { Snippet } from 'svelte';
	/** 페이지 헤더 — h2 제목 + 선택적 우측 액션 슬롯(모바일에서 줄바꿈).
	 * 전역 .page-head 스타일 사용. */
	let { title, actions }: { title: string; actions?: Snippet } = $props();
</script>

<div class="page-head">
	<h2>{title}</h2>
	{#if actions}<div class="actions">{@render actions()}</div>{/if}
</div>
