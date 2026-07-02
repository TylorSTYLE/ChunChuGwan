<script lang="ts">
	/** 전역 에러 화면 — 로드 실패(4xx/5xx) 시 레이아웃 안에서 스타일된 안내를 보여준다.
	 * SvelteKit 기본 평문("500 Internal Error") 대신 토큰 기반 카드로 렌더. */
	import { page } from '$app/state';
	import { resolve } from '$app/paths';
	import { t } from '$lib/i18n';

	const status = $derived(page.status);
	const message = $derived(page.error?.message);
</script>

<div class="err">
	<div class="code mono">{status}</div>
	<h2>{t('문제가 발생했습니다')}</h2>
	<p class="muted">{message || t('요청한 페이지를 표시할 수 없습니다.')}</p>
	<a href={resolve('/')}>← {t('현황')}</a>
</div>

<style>
	.err {
		max-width: 460px;
		margin: 9vh auto;
		text-align: center;
	}
	.code {
		font-size: 44px;
		font-weight: 700;
		color: var(--muted);
		line-height: 1;
	}
	.err h2 {
		margin: 10px 0 6px;
	}
	.err p {
		margin-bottom: 16px;
		overflow-wrap: anywhere;
	}
</style>
