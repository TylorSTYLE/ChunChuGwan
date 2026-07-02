<script lang="ts">
	import * as Tabs from '$lib/components/ui/tabs';
	/** 세그먼트 컨트롤 — 상호배타 옵션 선택(탭 형태). 선택 시 value 갱신.
	 * shadcn Tabs(List/Trigger)로 세그먼트 룩을 그대로 얻고 항상 하나가 선택된다. */
	type Opt = { value: string; label: string };
	let {
		value = $bindable(''),
		options
	}: { value?: string; options: Opt[] } = $props();
</script>

<Tabs.Root bind:value>
	<Tabs.List>
		{#each options as o (o.value)}
			<Tabs.Trigger value={o.value}>{o.label}</Tabs.Trigger>
		{/each}
	</Tabs.List>
</Tabs.Root>
