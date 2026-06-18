<script lang="ts">
	import { t } from '$lib/i18n';
	/** 페이지네이션 — 이전/다음 + 현재/전체. href(n) 로 각 페이지 URL 을 만든다
	 * (필터 파라미터 보존은 호출부 책임). 전역 .pager 스타일 사용. */
	let {
		page,
		totalPages,
		href
	}: { page: number; totalPages: number; href: (n: number) => string } = $props();
</script>

{#if totalPages > 1}
	<nav class="pager" aria-label={t('페이지 이동')}>
		{#if page > 1}<a href={href(page - 1)}>← {t('이전')}</a>{/if}
		<span class="muted">{page} / {totalPages}</span>
		{#if page < totalPages}<a href={href(page + 1)}>{t('다음')} →</a>{/if}
	</nav>
{/if}
