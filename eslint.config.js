import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  // `docs/archive` holds dated SNAPSHOTS of files taken before a sweep — a
  // frozen copy of AdmitPatientModal.tsx sits in there, and linting it counts
  // its problems twice and ratchets the baseline against code nobody will edit
  // again. Archives are evidence of what changed, not source. The rest of
  // docs/ is not source either: research and workflow scripts saved there
  // (some are fragments that do not parse) must not move the ratchet.
  // .claude holds local tool state, including whole
  // copies of src/ that would count every problem twice.
  globalIgnores(['dist', 'docs', '.claude']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
    },
    rules: {
      // A name that starts with `_` is unused on purpose (a mock's argument kept
      // for its signature), and so is the sibling a rest pattern drops
      // (`const { [id]: _gone, ...rest } = prev` is how a key is removed without
      // mutating). Everything else unused is still an error.
      '@typescript-eslint/no-unused-vars': ['error', {
        argsIgnorePattern: '^_',
        ignoreRestSiblings: true,
      }],
    },
  },
  {
    // App code only: console.log/info/debug in a clinical browser put
    // transcripts and request URLs carrying patient ids where any extension or
    // shared machine could read them. warn and error stay, for real failures.
    // Tests may print evidence.
    files: ['src/**/*.{ts,tsx}'],
    ignores: ['src/**/*.test.{ts,tsx}', 'src/test/**'],
    rules: {
      'no-console': ['error', { allow: ['warn', 'error'] }],
    },
  },
])
