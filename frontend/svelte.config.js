import adapter from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

/** @type {import('@sveltejs/kit').Config} */
const config = {
	preprocess: vitePreprocess(),
	kit: {
		// 정적 SPA — Node 런타임 없이 FastAPI 가 산출물을 서빙한다.
		// fallback(index.html)으로 클라이언트 라우팅·딥링크를 처리한다.
		adapter: adapter({
			pages: 'build',
			assets: 'build',
			fallback: 'index.html',
			precompress: false,
			strict: false
		}),
		// 공존 기간 — 기존 SSR 대시보드(/)와 충돌하지 않게 SPA 를 /ui 아래 둔다.
		// 빅뱅 컷오버 PR 에서 base 를 '' 로 바꾸고 / 로 서빙한다.
		paths: { base: '/ui' }
	}
};

export default config;
