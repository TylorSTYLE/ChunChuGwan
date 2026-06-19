<script lang="ts">
	/** 인라인 로딩 스피너 — 버튼 등 진행 중 표시에 곁들인다(글자 크기에 맞춰 size 조정). */
	let { size = 11 }: { size?: number } = $props();
</script>

<span class="spinner" aria-hidden="true" style="--size: {size}px"></span>

<style>
	.spinner {
		width: var(--size, 11px);
		height: var(--size, 11px);
		border: 2px solid var(--border);
		border-top-color: var(--fg);
		border-radius: 50%;
		animation: spin 0.7s linear infinite;
	}
	@keyframes spin {
		to {
			transform: rotate(360deg);
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.spinner {
			animation-duration: 2.5s;
		}
	}
</style>
