<script lang="ts">
	import { t } from '$lib/i18n';
	/** 단일 선택 칩 그룹 — select 대체. options 는 [value, 한국어 라벨] 튜플.
	 * 라벨은 변수라 i18n 정적 검사 대상이 아니므로 en 카탈로그를 직접 관리한다. */
	let {
		value = $bindable(''),
		options
	}: { value?: string; options: [string, string][] } = $props();
</script>

<div class="chips" role="radiogroup">
	{#each options as [v, label]}
		<button
			type="button"
			role="radio"
			aria-checked={value === v}
			class="chip"
			onclick={() => (value = v)}
		>
			{t(label)}
		</button>
	{/each}
</div>

<style>
	.chips {
		display: flex;
		flex-wrap: wrap;
		gap: 7px;
	}
	.chip {
		padding: 5px 13px;
		border: 1px solid var(--border);
		border-radius: 999px;
		background: var(--surface);
		color: var(--muted);
		font-size: 13px;
	}
	.chip:hover {
		background: var(--bg-soft);
		color: var(--fg);
	}
	.chip[aria-checked='true'] {
		background: var(--blue-bg);
		color: var(--blue);
		border-color: var(--blue);
	}
</style>
