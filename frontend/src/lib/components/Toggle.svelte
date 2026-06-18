<script lang="ts">
	/** 불리언 토글 스위치 — 체크박스 대체. 행 레이아웃(설명 좌 · 스위치 우).
	 * ON 트랙은 프라이머리 버튼과 같은 초록(#16a34a). */
	let {
		checked = $bindable(false),
		label = '',
		description = ''
	}: { checked?: boolean; label?: string; description?: string } = $props();
</script>

<div class="toggle-row">
	<span class="toggle-text">
		<span class="toggle-label">{label}</span>
		{#if description}<span class="toggle-desc">{description}</span>{/if}
	</span>
	<button
		type="button"
		role="switch"
		aria-checked={checked}
		aria-label={label}
		class="sw"
		onclick={() => (checked = !checked)}
	></button>
</div>

<style>
	.toggle-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 14px;
	}
	.toggle-text {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}
	.toggle-label {
		font-size: 13px;
	}
	.toggle-desc {
		font-size: 12px;
		color: var(--muted);
	}
	.sw {
		flex: none;
		width: 40px;
		height: 23px;
		padding: 0;
		border: 1px solid var(--border);
		border-radius: 12px;
		background: var(--bg-soft);
		position: relative;
		transition: background 0.15s;
	}
	.sw::after {
		content: '';
		position: absolute;
		top: 2px;
		left: 2px;
		width: 17px;
		height: 17px;
		border-radius: 50%;
		background: #fff;
		transition: left 0.15s;
	}
	.sw:hover {
		background: var(--border);
	}
	.sw[aria-checked='true'] {
		background: #16a34a;
		border-color: #16a34a;
	}
	.sw[aria-checked='true']:hover {
		background: #15803d;
	}
	.sw[aria-checked='true']::after {
		left: 19px;
	}
</style>
