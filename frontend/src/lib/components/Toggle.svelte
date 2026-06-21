<script lang="ts">
	import { Switch } from '$lib/components/ui/switch';
	/** 불리언 토글 스위치 — 체크박스 대체. 행 레이아웃(설명 좌 · 스위치 우).
	 * 색은 shadcn Switch 의 primary 토큰을 따른다(하드코딩 색 제거). */
	let {
		checked = $bindable(false),
		label = '',
		description = ''
	}: { checked?: boolean; label?: string; description?: string } = $props();
</script>

<div class="flex items-center justify-between gap-3.5">
	<span class="flex flex-col gap-0.5">
		<span class="text-[13px]">{label}</span>
		{#if description}<span class="text-xs text-muted-foreground">{description}</span>{/if}
	</span>
	<Switch bind:checked aria-label={label} />
</div>
