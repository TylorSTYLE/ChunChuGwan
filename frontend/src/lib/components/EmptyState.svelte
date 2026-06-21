<script lang="ts">
	/** 빈 상태 메시지 — 목록이 비었을 때. tag='tr' 이면 테이블 빈 행(colspan)으로. */
	let {
		message,
		tag = 'p',
		colspan = 1
	}: { message: string; tag?: 'p' | 'tr'; colspan?: number } = $props();
</script>

{#if tag === 'tr'}
	<tr><td {colspan} class="px-2.5 py-4 text-center text-muted-foreground">{message}</td></tr>
{:else}
	<p class="my-3 text-[13px] text-muted-foreground">{message}</p>
{/if}
