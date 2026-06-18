<script lang="ts">
	/** 알림 박스 — error/notice/warn 메시지 표시(비면 렌더 안 함).
	 * 스타일은 전역 .error/.notice/.warn (app.css) 재사용. */
	let {
		error = '',
		notice = '',
		warn = ''
	}: { error?: string; notice?: string; warn?: string } = $props();
</script>

{#if error}<div class="error">{error}</div>{/if}
{#if notice}<div class="notice">{notice}</div>{/if}
{#if warn}<div class="warn">{warn}</div>{/if}
