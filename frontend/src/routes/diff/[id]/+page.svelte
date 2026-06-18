<script lang="ts">
	import { pagePath } from '$lib/urls';
	import { base } from '$app/paths';
	import { t } from '$lib/i18n';
	import { ts } from '$lib/format';
	import type { DiffData } from '$lib/types';

	let { data }: { data: { diff: DiffData } } = $props();
	const d = $derived(data.diff);

	// 스크린샷 비교 모드 — 토글(나란히 / 픽셀 diff)
	let shotMode = $state<'side' | 'pixel'>('side');
</script>

<h2 class="mono">{d.page.url}</h2>

<div class="toolbar">
	<a href={pagePath(d.page.site_id, d.page.id)}>← {t('타임라인')}</a>
	<span class="muted"
		>#{d.from_idx} ({ts(String(d.old_snap.taken_at))}) → #{d.to_idx} ({ts(
			String(d.new_snap.taken_at)
		)})</span
	>
	<span class="spacer"></span>
	<span class="mono" style="color:var(--green)">+{d.added}</span>
	<span class="mono" style="color:var(--amber)">-{d.removed}</span>
</div>

<h3>{t('본문 비교')}</h3>
<table class="diff">
	<tbody>
		{#each d.rows as [tag, left, right]}
			{#if tag === 'skip'}
				<tr class="d-skip"><td colspan="2">… {left} …</td></tr>
			{:else}
				<tr class="d-{tag}">
					<td class="l mono">{left}</td>
					<td class="r mono">{right}</td>
				</tr>
			{/if}
		{/each}
	</tbody>
</table>

<h3>{t('스크린샷 비교')}</h3>
{#if d.local_capture}
	<p class="muted">
		{t('확장(브라우저) 캡처가 포함되어 스크린샷 비교는 제공하지 않습니다 (렌더 환경 차이).')}
	</p>
{:else}
	<div class="toolbar">
		<button class="tab" class:active={shotMode === 'side'} onclick={() => (shotMode = 'side')}
			>{t('나란히')}</button
		>
		<button class="tab" class:active={shotMode === 'pixel'} onclick={() => (shotMode = 'pixel')}
			>{t('픽셀 차이')}</button
		>
		{#if d.shot_ratio != null}
			<span class="muted">{t('차이')}: {(d.shot_ratio * 100).toFixed(2)}%</span>
		{/if}
	</div>
	{#if shotMode === 'side'}
		<div class="shot-grid">
			<img src={d.old_shot} alt="old" />
			<img src={d.new_shot} alt="new" />
		</div>
	{:else}
		<img src={d.shotdiff_url} alt="pixel diff" class="shot-diff" />
	{/if}
{/if}

<style>
	table.diff {
		table-layout: fixed;
		font-size: 12px;
	}
	table.diff td {
		width: 50%;
		vertical-align: top;
		white-space: pre-wrap;
		overflow-wrap: anywhere;
		padding: 1px 8px;
		border-bottom: none;
	}
	tr.d-delete td.l {
		background: var(--red-bg);
	}
	tr.d-replace td.l {
		background: var(--amber-bg);
	}
	tr.d-insert td.r,
	tr.d-replace td.r {
		background: var(--green-bg);
	}
	tr.d-skip td {
		text-align: center;
		color: var(--muted);
		background: var(--bg-soft);
		padding: 2px;
	}
	.shot-grid {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 10px;
	}
	.shot-grid img,
	.shot-diff {
		max-width: 100%;
		border: 1px solid var(--border);
	}
	button.tab.active {
		background: var(--gray-bg);
		border-color: var(--gray);
	}
	.toolbar .spacer {
		flex: 1;
	}
</style>
