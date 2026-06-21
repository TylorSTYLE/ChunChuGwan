<script lang="ts">
	import { t } from '$lib/i18n';
	/** 단일 선택 칩 그룹 — select 대체. options 는 [value, 한국어 라벨] 튜플.
	 * 라벨은 변수라 i18n 정적 검사 대상이 아니므로 en 카탈로그를 직접 관리한다. */
	let {
		value = $bindable(''),
		options
	}: { value?: string; options: [string, string][] } = $props();
</script>

<div class="flex flex-wrap gap-1.5" role="radiogroup">
	{#each options as [v, label]}
		<button
			type="button"
			role="radio"
			aria-checked={value === v}
			class="rounded-full border bg-background px-3 py-1 text-[13px] text-muted-foreground transition-colors hover:bg-muted hover:text-foreground aria-checked:border-running aria-checked:bg-running-bg aria-checked:text-running"
			onclick={() => (value = v)}
		>
			{t(label)}
		</button>
	{/each}
</div>
