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
			// 모든 내비게이션은 $app/paths 의 resolve() 를 거치고, 모든 #each 는 key 를 가진다.
			"svelte/require-each-key": "error",
			"svelte/no-navigation-without-resolve": "error"
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
