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
		// 빅뱅 컷오버(C2) 완료 — SSR 을 제거하고 SPA 를 루트(/)로 서빙한다.
		// FastAPI 는 정적 산출물 + /api/web JSON + 자원/CAS 만 담당한다.
		paths: { base: '' }
	}
};

export default config;
