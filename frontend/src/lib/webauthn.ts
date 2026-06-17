/** WebAuthn API(ArrayBuffer) ↔ 서버(py_webauthn, base64url) 변환 헬퍼.
 *
 * 기존 SSR `_webauthn.html` 의 b64uToBuf/bufToB64u 와 동일 로직을 TS 로 포팅.
 */

export function b64uToBuf(s: string): ArrayBuffer {
	s = s.replace(/-/g, '+').replace(/_/g, '/');
	const pad = s.length % 4 ? '='.repeat(4 - (s.length % 4)) : '';
	return Uint8Array.from(atob(s + pad), (c) => c.charCodeAt(0)).buffer;
}

export function bufToB64u(buf: ArrayBuffer): string {
	let bin = '';
	new Uint8Array(buf).forEach((b) => (bin += String.fromCharCode(b)));
	return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}
