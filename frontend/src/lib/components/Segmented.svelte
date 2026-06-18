<script lang="ts">
	/** 세그먼트 컨트롤 — 상호배타 옵션 선택(탭 형태). 선택 시 value 갱신. */
	type Opt = { value: string; label: string };
	let {
		value = $bindable(''),
		options
	}: { value?: string; options: Opt[] } = $props();
</script>

<div class="seg" role="tablist">
	{#each options as o}
		<button
			type="button"
			role="tab"
			aria-selected={value === o.value}
			onclick={() => (value = o.value)}
		>
			{o.label}
		</button>
	{/each}
</div>

<style>
	.seg {
		display: inline-flex;
		gap: 3px;
		padding: 3px;
		background: var(--bg-soft);
		border-radius: 6px;
	}
	.seg button {
		border: none;
		background: transparent;
		padding: 6px 16px;
		border-radius: 4px;
		font-size: 13px;
		color: var(--muted);
	}
	.seg button:hover {
		background: transparent;
		color: var(--fg);
	}
	.seg button[aria-selected='true'] {
		background: var(--surface);
		color: var(--fg);
		box-shadow: 0 1px 2px rgba(0, 0, 0, 0.08);
	}
</style>
