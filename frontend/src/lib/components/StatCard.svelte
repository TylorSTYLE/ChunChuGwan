<script lang="ts">
	/** 통계 카드 — 라벨 + 값. 전역 .stat-card 스타일 재사용. */
	let { label, value }: { label: string; value: string | number } = $props();
</script>

<div class="stat-card">
	<div class="label">{label}</div>
	<div class="value">{value}</div>
</div>
