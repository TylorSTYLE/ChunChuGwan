<script lang="ts">
	import { t } from '$lib/i18n';
	import { Badge } from '$lib/components/ui/badge';
	/** URL 호스트 분류 인라인 배지 — 춘추관 상태 색 variant 재사용.
	 * 라벨을 리터럴 t() 로 두어 i18n 정적 검사가 en 카탈로그를 강제하게 한다. */
	let { kind }: { kind: 'public' | 'private' | 'loopback' } = $props();
</script>

{#if kind === 'public'}
	<Badge variant="new" class="self-start">{t('공개 주소')}</Badge>
{:else if kind === 'private'}
	<Badge variant="changed" class="self-start">{t('사설 IP — 태그 필요')}</Badge>
{:else if kind === 'loopback'}
	<Badge variant="error" class="self-start">{t('루프백 — 아카이빙 불가')}</Badge>
{/if}
