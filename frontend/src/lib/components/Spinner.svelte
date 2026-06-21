<script lang="ts">
	import LoaderCircle from '@lucide/svelte/icons/loader-circle';
	/** 인라인 로딩 스피너 — 버튼 등 진행 중 표시에 곁들인다(글자 크기에 맞춰 size 조정). */
	let { size = 12 }: { size?: number } = $props();
</script>

<LoaderCircle
	{size}
	class="inline shrink-0 animate-spin text-muted-foreground motion-reduce:[animation-duration:2.5s]"
	aria-hidden="true"
/>
