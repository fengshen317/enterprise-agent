import js from '@eslint/js'
import tseslint from 'typescript-eslint'
import vue from 'eslint-plugin-vue'

export default tseslint.config(
  { ignores: ['dist/**', 'node_modules/**'] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  ...vue.configs['flat/recommended'],
  { files: ['**/*.vue'], languageOptions: { parserOptions: { parser: tseslint.parser } } },
  { files: ['**/*.vue', '**/*.ts'], rules: { 'no-undef': 'off' } },
  { rules: { 'vue/html-self-closing': ['error', { html: { void: 'always', normal: 'always', component: 'always' } }] } },
)
