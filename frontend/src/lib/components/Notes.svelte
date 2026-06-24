<script lang="ts">
	/** 페이지·사이트 메모 — 누적 표시 + 등록/개별 삭제 (권한 게이트).
	 *
	 * 메모가 있으면 화면 위쪽에 시간순으로 보여주고, 등록 권한이 있으면 하단에
	 * 입력란을, 삭제 권한이 있으면 항목마다 삭제 버튼을 둔다. 권한 플래그는
	 * 호출처(타임라인/사이트 상세 응답)가 내려준 can_memo_* 를 그대로 받는다. */
	import { t } from '$lib/i18n';
	import { ts } from '$lib/format';
	import { api } from '$lib/api';
	import { createAction } from '$lib/action.svelte';
	import AlertBox from '$lib/components/AlertBox.svelte';
	import { Button } from '$lib/components/ui/button';
	import { Textarea } from '$lib/components/ui/textarea';
	import type { Note } from '$lib/types';

	let {
		kind,
		targetId,
		notes,
		canView = false,
		canCreate = false,
		canDelete = false
	}: {
		kind: 'page' | 'site';
		targetId: number;
		notes: Note[];
		canView?: boolean;
		canCreate?: boolean;
		canDelete?: boolean;
	} = $props();

	const action = createAction();
	let draft = $state('');
	const basePath = $derived(kind === 'page' ? `/pages/${targetId}/notes` : `/sites/${targetId}/notes`);

	async function add() {
		const content = draft.trim();
		if (!content) return;
		await action.run(() => api(basePath, { method: 'POST', body: JSON.stringify({ content }) }));
		if (!action.error) draft = '';
	}

	function remove(id: number) {
		if (!confirm(t('이 메모를 삭제할까요?'))) return;
		action.run(() => api(`/notes/${id}`, { method: 'DELETE' }));
	}
</script>

{#if canView && (notes.length > 0 || canCreate)}
	<section class="my-3 rounded-lg border bg-muted/30 p-3.5">
		<h3 class="mb-2.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
			{t('메모')}{#if notes.length > 0}&nbsp;({notes.length}){/if}
		</h3>
		<AlertBox error={action.error} />
		{#if notes.length > 0}
			<ul class="flex flex-col gap-2">
				{#each notes as note (note.id)}
					<li class="rounded-md border bg-background p-2.5">
						<div class="mb-1 flex items-center justify-between gap-2">
							<span class="text-xs text-muted-foreground">
								<span class="mono">{ts(note.created_at)}</span>
								· {note.author_label}
							</span>
							{#if canDelete}
								<Button
									variant="ghost"
									size="sm"
									class="h-6 px-2 text-xs text-danger"
									onclick={() => remove(note.id)}
									disabled={action.busy}>{t('삭제')}</Button
								>
							{/if}
						</div>
						<p class="whitespace-pre-wrap break-words text-sm">{note.content}</p>
					</li>
				{/each}
			</ul>
		{/if}
		{#if canCreate}
			<div class="mt-3 flex flex-col gap-2">
				<Textarea
					bind:value={draft}
					rows={2}
					placeholder={t('메모를 입력하세요')}
					disabled={action.busy}
				/>
				<div>
					<Button size="sm" onclick={add} disabled={action.busy || !draft.trim()}>
						{t('메모 등록')}
					</Button>
				</div>
			</div>
		{/if}
	</section>
{/if}
