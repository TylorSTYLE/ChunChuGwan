<script lang="ts">
	import { t } from '$lib/i18n';
	/** URL 호스트 분류 인라인 배지 — 전역 .badge 스타일(green/amber/red) 재사용.
	 * 라벨을 리터럴 t() 로 두어 i18n 정적 검사가 en 카탈로그를 강제하게 한다. */
	let { kind }: { kind: 'public' | 'private' | 'loopback' } = $props();
</script>

{#if kind === 'public'}
	<span class="badge new">{t('공개 주소')}</span>
{:else if kind === 'private'}
	<span class="badge changed">{t('사설 IP — 태그 필요')}</span>
{:else if kind === 'loopback'}
	<span class="badge error">{t('루프백 — 아카이빙 불가')}</span>
{/if}

<style>
	.badge {
		align-self: flex-start;
	}
</style>
