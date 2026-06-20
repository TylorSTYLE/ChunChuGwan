<script lang="ts">
	/** 업데이트 안내 모달 — 로그인 후 현재 버전 노트를 1회 가운데 띄운다.
	 * 본문(title·items)은 백엔드가 로케일에 맞춰 내려준 평문이라 t() 를 거치지
	 * 않는다(UI 라벨만 번역). 배경 클릭·Esc·닫기 버튼으로 닫는다 — "봤음" 기록은
	 * 호출 측(+layout)이 onclose 에서 localStorage 에 남긴다.
	 *
	 * 배경(backdrop)은 빈 <button> 으로 둔다 — 정적 div + onclick 의 a11y 경고를
	 * 피하면서 마우스로 바깥을 눌러 닫게 한다. tabindex=-1 로 탭 순서에서 빼고,
	 * 키보드 사용자는 ×·닫기 버튼과 Esc 로 닫는다. */
	import { t } from '$lib/i18n';
	import type { ReleaseNote } from '$lib/types';

	let { note, onclose }: { note: ReleaseNote; onclose: () => void } = $props();

	// Esc 로 닫기 + 떠 있는 동안 배경 스크롤 잠금.
	$effect(() => {
		function onKey(e: KeyboardEvent) {
			if (e.key === 'Escape') onclose();
		}
		document.addEventListener('keydown', onKey);
		const prev = document.body.style.overflow;
		document.body.style.overflow = 'hidden';
		return () => {
			document.removeEventListener('keydown', onKey);
			document.body.style.overflow = prev;
		};
	});
</script>

<div class="modal-backdrop">
	<button type="button" class="backdrop-close" tabindex="-1" aria-label={t('닫기')} onclick={onclose}
	></button>
	<div class="modal" role="dialog" aria-modal="true" aria-labelledby="update-note-title">
		<div class="modal-head">
			<h2 id="update-note-title">
				{note.title} <span class="mono muted">v{note.version}</span>
			</h2>
			<button type="button" class="icon-close" aria-label={t('닫기')} onclick={onclose}>×</button>
		</div>
		<ul class="modal-items">
			{#each note.items as item}
				<li>
					{item.text}{#if item.url}{' '}<a
							class="pr-link"
							href={item.url}
							target="_blank"
							rel="noopener noreferrer"
							title="PR #{item.pr}">#{item.pr}</a
						>{/if}
				</li>
			{/each}
		</ul>
		<div class="modal-foot">
			<button type="button" class="primary" onclick={onclose}>{t('닫기')}</button>
		</div>
	</div>
</div>

<style>
	.modal-backdrop {
		position: fixed;
		inset: 0;
		z-index: 100;
		display: flex;
		align-items: center;
		justify-content: center;
		padding: 16px;
		background: rgba(0, 0, 0, 0.5);
	}
	/* 배경 전체를 덮는 투명 버튼 — 바깥 클릭으로 닫기. 모달은 그 위(z-index)로. */
	.backdrop-close {
		position: absolute;
		inset: 0;
		margin: 0;
		padding: 0;
		border: none;
		background: transparent;
		cursor: default;
	}
	.backdrop-close:hover {
		background: transparent;
	}
	.modal {
		position: relative;
		z-index: 1;
		width: 100%;
		max-width: 560px;
		max-height: 85vh;
		overflow-y: auto;
		background: var(--surface);
		border: 1px solid var(--border);
		border-top: 3px solid var(--seal);
		border-radius: 8px;
		padding: 22px 26px;
		box-shadow: 0 10px 30px rgba(0, 0, 0, 0.18);
	}
	.modal-head {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: 12px;
		margin-bottom: 14px;
	}
	.modal-head h2 {
		margin: 0;
		font-size: 17px;
	}
	.icon-close {
		flex: none;
		border: none;
		background: none;
		font-size: 22px;
		line-height: 1;
		padding: 0 4px;
		color: var(--muted);
	}
	.icon-close:hover {
		background: none;
		color: var(--fg);
	}
	.modal-items {
		margin: 0;
		padding-left: 20px;
		font-size: 14px;
		line-height: 1.75;
	}
	.modal-items li {
		margin-bottom: 6px;
	}
	/* PR 참조 링크 — 본문 옆 부차 정보라 모노스페이스·muted, 호버 시 강조. */
	.pr-link {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: 12px;
		color: var(--muted);
		white-space: nowrap;
	}
	.pr-link:hover {
		color: var(--link);
		text-decoration: underline;
	}
	.modal-foot {
		display: flex;
		justify-content: flex-end;
		margin-top: 20px;
	}

	/* 모바일 — 배경 여백을 줄여 모달 폭을 최대한 확보하고 안쪽 패딩도 축소. */
	@media (max-width: 599px) {
		.modal-backdrop {
			padding: 12px;
		}
		.modal {
			padding: 18px 18px;
		}
		.modal-head h2 {
			font-size: 16px;
		}
	}
</style>
