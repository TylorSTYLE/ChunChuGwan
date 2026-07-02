import js from "@eslint/js";
import svelte from "eslint-plugin-svelte";
import globals from "globals";
import ts from "typescript-eslint";
import svelteConfig from "./svelte.config.js";

export default ts.config(
	js.configs.recommended,
	...ts.configs.recommended,
	...svelte.configs.recommended,
	{
		languageOptions: {
			globals: { ...globals.browser, ...globals.node }
		},
		rules: {
			// 프로젝트 컨벤션 — 의도적으로 안 쓰는 구조분해/매개변수는 `_` 접두사로 표기한다.
			"@typescript-eslint/no-unused-vars": [
				"error",
				{ argsIgnorePattern: "^_", varsIgnorePattern: "^_", destructuredArrayIgnorePattern: "^_" }
			],
			// 부트스트랩 시점 기존 위반 다수(각 130건 이상, 대부분 기존 라우트 전반) 동결.
			// 점진 해소 대상은 CLAUDE.md 마이그레이션 백로그 참조 — 새 코드는 두 규칙 모두 준수할 것.
			"svelte/require-each-key": "warn",
			"svelte/no-navigation-without-resolve": "warn"
		}
	},
	{
		files: ["**/*.svelte", "**/*.svelte.ts", "**/*.svelte.js"],
		languageOptions: {
			parserOptions: {
				projectService: true,
				extraFileExtensions: [".svelte"],
				parser: ts.parser,
				svelteConfig
			}
		}
	},
	{
		ignores: ["build/", ".svelte-kit/", "dist/", "node_modules/"]
	}
);
