<script lang="ts">
	import { t } from '$lib/i18n';
	import { buttonVariants } from '$lib/components/ui/button';
	import { cn } from '$lib/utils';
	import type { ResolvedPathname } from '$app/types';
	/** 페이지네이션 — 이전/다음 + 현재/전체.
	 *
	 * href(n) 로 각 페이지 URL 을 만든다(필터 파라미터 보존은 호출부 책임). onpage 를 주면
	 * 일반 좌클릭을 가로채 리로드 없이 in-place 갱신하고(수식키·새 탭·휠클릭은 href 로 실제
	 * 이동해 하위 호환 유지), busy 동안에는 중복 클릭을 막는다. */
	let {
		page,
		totalPages,
		href,
		onpage,
		busy = false
	}: {
		page: number;
		totalPages: number;
		href: (n: number) => ResolvedPathname;
		onpage?: (n: number) => void;
		busy?: boolean;
	} = $props();

	function nav(e: MouseEvent, n: number) {
		if (!onpage) return; // href 폴백 — 기본 네비게이션
		// 수식키/새 탭/휠클릭 등은 브라우저 기본 동작(href) 유지
		if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
		e.preventDefault();
		if (busy) return;
		onpage(n);
	}

	const linkCls = cn(buttonVariants({ variant: 'outline', size: 'sm' }));
</script>

{#if totalPages > 1}
	<nav
		class="mt-3 flex flex-wrap items-center gap-3 {busy ? 'pointer-events-none opacity-50' : ''}"
		aria-label={t('페이지 이동')}
		aria-busy={busy}
	>
		{#if page > 1}
			<a href={href(page - 1)} onclick={(e) => nav(e, page - 1)} class={linkCls}>← {t('이전')}</a>
		{/if}
		<span class="text-sm tabular-nums text-muted-foreground">{page} / {totalPages}</span>
		{#if page < totalPages}
			<a href={href(page + 1)} onclick={(e) => nav(e, page + 1)} class={linkCls}>{t('다음')} →</a>
		{/if}
	</nav>
{/if}
