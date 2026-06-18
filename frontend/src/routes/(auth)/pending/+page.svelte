<script lang="ts">
	import { t } from '$lib/i18n';

	let { data }: { data: { email: string } } = $props();
</script>

<div class="auth-card">
	<h2>{t('가입 승인 대기 중')}</h2>
	<p class="lead">{t('가입해 주셔서 감사합니다. 현재 계정은 관리자의 승인을 기다리고 있습니다.')}</p>
	<p class="muted sm">
		{t('관리자가 권한을 부여하면 바로 이용할 수 있습니다. 잠시 후 다시 방문해 주세요.')}
	</p>
	<div class="mono muted email">{data.email}</div>
	<form method="POST" action="/logout"><button type="submit">{t('로그아웃')}</button></form>
</div>

<style>
	.lead {
		font-size: 13px;
		line-height: 1.7;
		margin: 0 0 10px;
	}
	.sm {
		font-size: 12px;
		line-height: 1.7;
		margin: 0 0 14px;
	}
	.email {
		font-size: 12px;
		margin-bottom: 14px;
	}
</style>
