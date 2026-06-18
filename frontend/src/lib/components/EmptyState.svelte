<script lang="ts">
	/** 빈 상태 메시지 — 목록이 비었을 때. tag='tr' 이면 테이블 빈 행(colspan)으로. */
	let {
		message,
		tag = 'p',
		colspan = 1
	}: { message: string; tag?: 'p' | 'tr'; colspan?: number } = $props();
</script>

{#if tag === 'tr'}
	<tr><td {colspan} class="muted empty-cell">{message}</td></tr>
{:else}
	<p class="muted empty-state">{message}</p>
{/if}

<style>
	.empty-state {
		font-size: 13px;
		margin: 12px 0;
	}
	.empty-cell {
		text-align: center;
		color: var(--muted);
		padding: 16px 10px;
	}
</style>
