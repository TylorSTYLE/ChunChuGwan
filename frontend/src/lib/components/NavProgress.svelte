<script lang="ts">
	/** 화면 전환(라우트 load 대기) 인디케이터.
	 *
	 * SvelteKit `navigating` 이 truthy 인 동안 상단 진행바를 띄우고, 화면 전체를
	 * 덮는 투명 오버레이로 이전 화면에서의 클릭 등 사용자 액션을 막는다. 데이터는
	 * 각 라우트 +page.ts load() 에서 받으므로 그 대기 구간이 곧 전환 구간이다.
	 * 짧은 전환은 깜빡임을 막으려 150ms 지연 후에만 표시한다. */
	import { navigating } from '$app/stores';
	import { t } from '$lib/i18n';

	let visible = $state(false);
	let timer: ReturnType<typeof setTimeout> | undefined;

	$effect(() => {
		const nav = $navigating;
		clearTimeout(timer);
		if (nav) {
			timer = setTimeout(() => (visible = true), 150);
		} else {
			visible = false;
		}
	});
</script>

{#if visible}
	<!-- 클릭 차단 오버레이 — 전환 완료 전 이전 화면 조작 방지 -->
	<div class="nav-overlay" role="status" aria-busy="true" aria-label={t('불러오는 중…')}></div>
	<div class="nav-bar" aria-hidden="true"><div class="nav-bar-fill"></div></div>
{/if}

<style>
	.nav-overlay {
		position: fixed;
		inset: 0;
		z-index: 90;
		cursor: progress;
		background: transparent;
	}
	.nav-bar {
		position: fixed;
		top: 0;
		left: 0;
		right: 0;
		height: 3px;
		z-index: 100;
		overflow: hidden;
		background: transparent;
	}
	.nav-bar-fill {
		height: 100%;
		width: 40%;
		background: var(--primary);
		border-radius: 0 2px 2px 0;
		animation: nav-slide 1s ease-in-out infinite;
	}
	@keyframes nav-slide {
		0% {
			transform: translateX(-100%);
		}
		100% {
			transform: translateX(350%);
		}
	}
</style>
