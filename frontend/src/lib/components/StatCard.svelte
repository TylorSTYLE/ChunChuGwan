<script lang="ts">
	/** 통계 카드 — 라벨 + 값. */
	let { label, value }: { label: string; value: string | number } = $props();
</script>

<div class="rounded-lg border px-3.5 py-2.5">
	<div class="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">{label}</div>
	<div class="mt-0.5 text-[22px] font-semibold tabular-nums max-[599px]:text-[19px]">{value}</div>
</div>
