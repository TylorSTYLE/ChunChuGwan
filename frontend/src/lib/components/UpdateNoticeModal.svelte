<script lang="ts">
	/** 업데이트 안내 모달 — 로그인 후 현재 버전 노트를 1회 가운데 띄운다(shadcn Dialog).
	 * 본문(title·items)은 백엔드가 로케일에 맞춰 내려준 평문이라 t() 를 거치지 않는다(UI
	 * 라벨만 번역). 배경 클릭·Esc·닫기 버튼으로 닫힌다 — "봤음" 기록은 호출 측(+layout)이
	 * onclose 에서 localStorage 에 남긴다. */
	import { t } from '$lib/i18n';
	import * as Dialog from '$lib/components/ui/dialog';
	import { Button } from '$lib/components/ui/button';
	import type { ReleaseNote } from '$lib/types';

	let { note, onclose }: { note: ReleaseNote; onclose: () => void } = $props();

	let open = $state(true);
	function onOpenChange(v: boolean) {
		if (!v) onclose();
	}
</script>

<Dialog.Root bind:open {onOpenChange}>
	<Dialog.Content class="max-h-[85vh] gap-4 overflow-y-auto border-t-[3px] border-t-seal sm:max-w-[560px]">
		<Dialog.Header>
			<Dialog.Title class="flex items-baseline gap-2">
				{note.title}
				<span class="font-mono text-sm font-normal text-muted-foreground">v{note.version}</span>
			</Dialog.Title>
		</Dialog.Header>
		<ul class="list-disc pl-5 text-sm leading-7">
			{#each note.items as item}
				<li>
					{item.text}{#if item.url} <a
							class="whitespace-nowrap font-mono text-xs text-muted-foreground hover:text-link hover:underline"
							href={item.url}
							target="_blank"
							rel="noopener noreferrer"
							title="PR #{item.pr}">#{item.pr}</a
						>{/if}
				</li>
			{/each}
		</ul>
		<Dialog.Footer>
			<!-- open=false 를 외부 대입하면 bits-ui 가 onOpenChange 를 부르지 않아 "봤음"
			     기록(onclose)이 안 남고 모달이 매번 다시 뜬다 — onclose 를 직접 호출한다.
			     (Esc·오버레이·X 경로에서 onOpenChange 가 또 불려도 기록은 멱등) -->
			<Button
				onclick={() => {
					open = false;
					onclose();
				}}>{t('닫기')}</Button>
		</Dialog.Footer>
	</Dialog.Content>
</Dialog.Root>
