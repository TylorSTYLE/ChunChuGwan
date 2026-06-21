/** 텍스트를 클립보드에 복사한다. 보안 컨텍스트(HTTPS·localhost)면
 * Clipboard API 를, 아니면(평문 HTTP 외부 노출 등) execCommand 로 폴백한다.
 * 성공 여부를 반환한다. */
export async function copyText(text: string): Promise<boolean> {
	try {
		if (navigator.clipboard?.writeText) {
			await navigator.clipboard.writeText(text);
			return true;
		}
	} catch {
		// Clipboard API 실패(권한 거부·비보안 컨텍스트) — execCommand 로 폴백
	}
	try {
		const ta = document.createElement('textarea');
		ta.value = text;
		ta.style.position = 'fixed';
		ta.style.left = '-9999px';
		document.body.appendChild(ta);
		ta.select();
		const ok = document.execCommand('copy');
		document.body.removeChild(ta);
		return ok;
	} catch {
		return false;
	}
}
