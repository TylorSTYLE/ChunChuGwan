<script lang="ts">
	import { t } from '$lib/i18n';
	/** 페이지네이션 — 이전/다음 + 현재/전체.
	 *
	 * href(n) 로 각 페이지 URL 을 만든다(필터 파라미터 보존은 호출부 책임). onpage 를 주면
	 * 일반 좌클릭을 가로채 리로드 없이 in-place 갱신하고(수식키·새 탭·휠클릭은 href 로 실제
	 * 이동해 하위 호환 유지), busy 동안에는 중복 클릭을 막는다. 전역 .pager 스타일 사용. */
	let {
		page,
		totalPages,
		href,
		onpage,
		busy = false
	}: {
		page: number;
		totalPages: number;
		href: (n: number) => string;
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
</script>

{#if totalPages > 1}
	<nav class="pager" aria-label={t('페이지 이동')} aria-busy={busy}>
		{#if page > 1}<a href={href(page - 1)} onclick={(e) => nav(e, page - 1)}>← {t('이전')}</a>{/if}
		<span class="muted">{page} / {totalPages}</span>
		{#if page < totalPages}<a href={href(page + 1)} onclick={(e) => nav(e, page + 1)}>{t('다음')} →</a
			>{/if}
	</nav>
{/if}

<style>
	/* in-place 갱신 중에는 페이저를 흐리고 클릭을 막아 중복 요청을 방지 */
	nav[aria-busy='true'] a {
		pointer-events: none;
		opacity: 0.5;
	}
</style>
