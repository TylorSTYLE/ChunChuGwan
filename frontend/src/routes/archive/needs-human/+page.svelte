<script lang="ts">
	import { onMount } from 'svelte';
	import { base } from '$app/paths';
	import { invalidateAll } from '$app/navigation';
	import { t } from '$lib/i18n';
	import { ts } from '$lib/format';
	import { api } from '$lib/api';
	import type { LiveJob } from '$lib/types';

	let { data }: { data: { jobs: LiveJob[] } } = $props();
	const jobs = $derived(data.jobs);

	// 라이브 세션은 비동기로 상태가 바뀐다(통과·취소·타임아웃). 대기 집합이 바뀌면
	// 목록을 새로 불러 낡은 '처리' 링크 클릭을 줄인다.
	onMount(() => {
		const key = () => jobs.map((j) => j.url).join('\n');
		const timer = setInterval(async () => {
			if (typeof document !== 'undefined' && document.hidden) return;
			try {
				const fresh = (await api<{ jobs: LiveJob[] }>('/live')).jobs;
				if (fresh.map((j) => j.url).join('\n') !== key()) await invalidateAll();
			} catch {
				/* 일시 오류 — 다음 폴링에서 회복 */
			}
		}, 3000);
		return () => clearInterval(timer);
	});
</script>

<div class="toolbar">
	<h2>{t('사람 확인 필요')}</h2>
	<span class="muted">{t('자동으로 통과하지 못한 챌린지 — 직접 풀어서 통과시킵니다')}</span>
</div>

{#if jobs.length === 0}
	<p class="muted">{t('사람 확인이 필요한 작업이 없습니다.')}</p>
{:else}
	<div class="table-wrap wide">
		<table>
			<thead>
				<tr><th>URL</th><th>{t('진입 시각')}</th><th></th></tr>
			</thead>
			<tbody>
				{#each jobs as j}
					<tr>
						<td>{j.url}</td>
						<td class="mono muted">{ts(j.needs_human_at)}</td>
						<td>
							<a href="{base}/archive/jobs/{j.id}/live">{t('처리')}</a>
							{#if j.held_by_other}<span class="muted">({t('처리 중')})</span>{/if}
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
{/if}

<style>
	.toolbar {
		display: flex;
		gap: 12px;
		align-items: baseline;
		flex-wrap: wrap;
		margin-bottom: 12px;
	}
	.toolbar h2 {
		margin: 0;
	}
</style>
