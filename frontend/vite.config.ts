import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export default defineConfig({
	plugins: [sveltekit()],
	server: {
		// 개발 시 FastAPI(8765)로 API·파일·CAS 서빙을 프록시한다.
		// 운영에서는 FastAPI 가 빌드 정적본을 직접 서빙하므로 프록시가 필요 없다.
		proxy: {
			'/api': 'http://127.0.0.1:8765',
			'/snapshot': 'http://127.0.0.1:8765',
			'/resource': 'http://127.0.0.1:8765',
			'/document': 'http://127.0.0.1:8765',
			'/auth': 'http://127.0.0.1:8765',
			'/favicon.svg': 'http://127.0.0.1:8765'
		}
	}
});
