<script lang="ts">
	import { t } from '$lib/i18n';
	/** 페이지당 항목 수 선택 — 변경 시 onchange(n) 으로 새 크기를 알린다.
	 *
	 * 목록 화면의 Toolbar 안(또는 페이저 근처)에 두는 밀도 있는 bare <select>.
	 * 호출부가 보통 page=1 로 리셋하며 list.go 한다. 전역 select 스타일을 따른다. */
	let {
		value,
		onchange,
		options = [10, 25, 50, 100]
	}: {
		value: number;
		onchange: (n: number) => void;
		options?: number[];
	} = $props();
</script>

<select
	class="page-size"
	aria-label={t('페이지당 항목 수')}
	value={String(value)}
	onchange={(e) => onchange(Number(e.currentTarget.value))}
>
	{#each options as n (n)}<option value={String(n)}>{n}</option>{/each}
</select>
