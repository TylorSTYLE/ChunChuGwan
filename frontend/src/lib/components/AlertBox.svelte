<script lang="ts">
	/** 알림 박스 — error/notice/warn 메시지 표시(비면 렌더 안 함).
	 * 춘추관 시맨틱 색(위험/신규/변경) 유틸을 쓴다. */
	let {
		error = '',
		notice = '',
		warn = ''
	}: { error?: string; notice?: string; warn?: string } = $props();
</script>

<!-- role="alert"(assertive) 로 오류를, notice/warn 은 aria-live="polite" 로 스크린리더에
     즉시/차례로 통보한다 — 액션 성공·실패 피드백이 낭독되지 않던 문제 보정. -->
{#if error}<div role="alert" class="mb-3 rounded-md bg-danger-bg px-3 py-2 text-sm text-danger">
		{error}
	</div>{/if}
{#if notice}<div role="status" aria-live="polite" class="mb-3 rounded-md bg-new-bg px-3 py-2 text-sm text-new">
		{notice}
	</div>{/if}
{#if warn}<div role="status" aria-live="polite" class="mb-3 rounded-md bg-changed-bg px-3 py-2 text-sm text-changed">
		{warn}
	</div>{/if}
