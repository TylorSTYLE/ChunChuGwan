/** 표시 포맷 헬퍼 — 기존 templating.py(filesize)·base.html(타임존 변환) 이식.
 *
 * 타임존은 사용자별(IANA)이라 모듈 상태로 들고 다닌다. 레이아웃이 /api/web/me
 * 의 timezone 으로 `setTimezone` 을 호출해 초기화한다 (미설정 시 UTC).
 */

let currentTz = 'UTC';

export function setTimezone(tz: string | null | undefined): void {
	currentTz = tz || 'UTC';
}

/** 바이트 수를 사람이 읽는 단위로 (예: 532 B, 1.4 KB, 2.0 MB) — filesize 이식. */
export function filesize(num: number | null | undefined): string {
	if (num == null) return '-';
	let size = num;
	if (size < 1024) return `${Math.floor(size)} B`;
	for (const unit of ['KB', 'MB', 'GB']) {
		size /= 1024;
		if (size < 1024 || unit === 'GB') return `${size.toFixed(1)} ${unit}`;
	}
	return `${size.toFixed(1)} GB`;
}

/** UTC ISO 타임스탬프를 사용자 타임존 표기로 (base.html 의 time.ts 변환 이식).
 *  sv-SE 로케일로 YYYY-MM-DD (HH:MM:SS) 형식을 얻는다. */
export function ts(iso: string | null | undefined, dateOnly = false): string {
	if (!iso) return '-';
	const d = new Date(iso);
	if (isNaN(d.getTime())) return String(iso);
	const opts: Intl.DateTimeFormatOptions = {
		timeZone: currentTz,
		year: 'numeric',
		month: '2-digit',
		day: '2-digit'
	};
	if (!dateOnly) {
		opts.hour = '2-digit';
		opts.minute = '2-digit';
		opts.second = '2-digit';
		opts.hour12 = false;
	}
	return d.toLocaleString('sv-SE', opts).replace(', ', ' ');
}
