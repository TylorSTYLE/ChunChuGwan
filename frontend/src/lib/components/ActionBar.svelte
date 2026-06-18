<script lang="ts">
	import type { Snippet } from 'svelte';
	/** 액션 버튼 바 — 여러 버튼/컨트롤을 좁은 화면에서 줄바꿈(전역 .action-bar). */
	let { children }: { children: Snippet } = $props();
</script>

<div class="action-bar">{@render children()}</div>
