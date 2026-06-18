<script lang="ts">
	import { pagePath, snapPath } from '$lib/urls';
	import { goto } from '$app/navigation';
	import { t } from '$lib/i18n';
	import { ts } from '$lib/format';
	import { filterUrl } from '$lib/filters';
	import type { SearchData } from '$lib/types';
	import Pager from '$lib/components/Pager.svelte';

	let { data }: { data: { search: SearchData } } = $props();
	const s = $derived(data.search);

	let q = $state('');
	let domain = $state('');
	let latest = $state(false);
	// load 결과가 바뀌면 폼 입력을 동기화
	$effect(() => {
		q = s.q;
		domain = s.domain;
		latest = s.latest;
	});

	function submit(e: Event) {
		e.preventDefault();
		goto(filterUrl('/search', { q: q.trim(), domain: domain.trim(), latest: latest ? '1' : '' }));
	}

	const pageUrl = (n: number) =>
		filterUrl('/search', { q: s.q, domain: s.domain, latest: s.latest ? '1' : '', page: n }, { page: 1 });
</script>

<h2>{t('검색')}</h2>

<form onsubmit={submit} class="toolbar">
	<input type="text" class="search-q" bind:value={q} placeholder={t('아카이브 본문·문서에서 검색…')} />
	<input type="text" class="search-domain" bind:value={domain} placeholder={t('도메인')} />
	<label class="muted"><input type="checkbox" bind:checked={latest} /> {t('최신만')}</label>
	<button type="submit" class="primary">{t('검색')}</button>
</form>

{#if !s.available}
	<p class="muted">{t('검색 인덱스가 아직 준비되지 않았습니다.')}</p>
{:else if !s.q}
	<p class="muted">{t('검색어를 입력하세요.')}</p>
{:else if !s.results || s.results.total === 0}
	<p class="muted">{t('검색 결과가 없습니다.')}</p>
{:else}
	<div class="results">
		<p class="muted count">{t('총')} {s.results.total}{t('건')}</p>
		{#each s.results.hits as hit}
			<div class="hit">
				<div class="mono hit-url">{hit.page_url}</div>
				<a href={snapPath(hit.site_id, hit.page_id, hit.snapshot_id)} class="hit-title">{hit.title || hit.page_url}</a>
				<div class="snippet">{hit.snippet}</div>
				<div class="muted hit-meta">
					{ts(hit.taken_at)} · <a href={pagePath(hit.site_id, hit.page_id)}>{t('타임라인')}</a>
				</div>
			</div>
		{/each}
		<Pager page={s.page} totalPages={s.total_pages} href={pageUrl} />
	</div>
{/if}

<style>
	.search-q {
		flex: 2 1 200px;
		min-width: 0;
	}
	.search-domain {
		flex: 1 1 140px;
		min-width: 0;
	}
	/* 검색엔진 결과창 스타일 — 가독 폭의 한 컬럼, URL → 큰 제목 링크 → 스니펫 순 */
	.results {
		max-width: 640px;
	}
	.count {
		font-size: 12px;
		margin: 4px 0 14px;
	}
	.hit {
		padding: 0 0 20px;
	}
	.hit-url {
		font-size: 12px;
		color: var(--green);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.hit-title {
		display: block;
		font-size: 18px;
		line-height: 1.3;
		margin: 1px 0 2px;
	}
	.hit-title:hover {
		text-decoration: underline;
	}
	.snippet {
		font-size: 13px;
		line-height: 1.6;
		color: var(--fg);
	}
	.snippet :global(mark) {
		background: var(--amber-bg);
		color: inherit;
	}
	.hit-meta {
		font-size: 12px;
		margin-top: 3px;
	}
</style>
