<script lang="ts">
	import { base } from '$app/paths';
	import { goto } from '$app/navigation';
	import { t } from '$lib/i18n';
	import { api, ApiError } from '$lib/api';

	let url = $state('');
	let force = $state(false);
	let site = $state(false);
	let interval = $state('0');
	let maxPages = $state('');
	let maxDepth = $state('');
	let delay = $state('');
	let error = $state('');
	let busy = $state(false);

	// 주기 선택지 — app._SCHEDULE_OPTIONS 와 동일(초 단위). "0" = 없음.
	const INTERVALS: [string, string][] = [
		['0', '없음'],
		['3600', '1시간'],
		['10800', '3시간'],
		['21600', '6시간'],
		['43200', '12시간'],
		['86400', '1일'],
		['259200', '3일'],
		['604800', '1주일'],
		['2592000', '1개월']
	];

	async function submit(e: Event) {
		e.preventDefault();
		if (!url.trim()) return;
		busy = true;
		error = '';
		try {
			const r = await api<{ site: boolean; crawl_id?: number }>('/archive', {
				method: 'POST',
				body: JSON.stringify({
					url: url.trim(),
					force,
					site,
					interval,
					crawl_max_pages: maxPages,
					crawl_max_depth: maxDepth,
					crawl_delay: delay
				})
			});
			// 단발은 목록으로, 사이트 전체는 사이트 목록으로 (크롤 진행 화면은 후속)
			goto(`${base}/archives`);
			void r;
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
			busy = false;
		}
	}
</script>

<h2>{t('새 아카이빙')}</h2>

{#if error}<div class="error">{error}</div>{/if}

<form onsubmit={submit} class="archive-form">
	<label>
		URL
		<input type="url" bind:value={url} placeholder="https://example.com" required />
	</label>

	<label class="check"><input type="checkbox" bind:checked={force} /> {t('콘텐츠 동일해도 강제 저장')}</label>
	<label class="check"><input type="checkbox" bind:checked={site} /> {t('사이트 전체 아카이브 (같은 호스트)')}</label>

	{#if site}
		<div class="crawl-opts">
			<label>{t('최대 페이지')}<input type="number" bind:value={maxPages} min="1" /></label>
			<label>{t('최대 깊이')}<input type="number" bind:value={maxDepth} min="0" /></label>
			<label>{t('지연(초)')}<input type="number" bind:value={delay} min="0" /></label>
		</div>
	{/if}

	<label>
		{t('자동 재아카이빙 주기')}
		<select bind:value={interval}>
			{#each INTERVALS as [v, label]}<option value={v}>{t(label)}</option>{/each}
		</select>
	</label>

	<p class="muted" style="font-size:12px">
		{t('사설 IP(로컬 네트워크) 대상은 네트워크 태그가 필요하며, 자격증명 연결은 추후 지원됩니다.')}
	</p>

	<button type="submit" class="primary" disabled={busy}>
		{busy ? t('등록 중…') : t('아카이빙 등록')}
	</button>
</form>

<style>
	.archive-form {
		max-width: 560px;
		display: flex;
		flex-direction: column;
		gap: 12px;
	}
	.archive-form label {
		display: flex;
		flex-direction: column;
		gap: 4px;
		font-size: 13px;
	}
	.archive-form label.check {
		flex-direction: row;
		align-items: center;
		gap: 6px;
	}
	.crawl-opts {
		display: flex;
		gap: 10px;
	}
	.crawl-opts label {
		flex: 1;
	}
	.error {
		background: var(--red-bg);
		color: var(--red-text);
		border-radius: 4px;
		padding: 8px 12px;
		margin-bottom: 12px;
		font-size: 13px;
	}
	button.primary {
		align-self: flex-start;
		color: #fff;
		background: #16a34a;
		border-color: #16a34a;
		padding: 6px 18px;
	}
	button.primary:hover {
		background: #15803d;
	}
	button.primary:disabled {
		opacity: 0.6;
	}
</style>
