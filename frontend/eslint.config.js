import js from "@eslint/js";
import tseslint from "typescript-eslint";
import prettier from "eslint-config-prettier";
import globals from "globals";

// Non-type-checked preset for now: it catches unused vars, undeclared globals, obvious
// mistakes, and typescript-eslint's syntactic rules without the ~50-finding backlog the
// type-checked preset flags (floating promises, unsafe `any` in refresh.ts). #242 PR D
// reworks the API/refresh layer and can move this to tseslint.configs.recommendedTypeChecked.
export default tseslint.config(
  {
    ignores: ["src/api/schema.ts", "dev-dist/**", "dist/**", "public/**"],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    languageOptions: {
      globals: { ...globals.browser },
    },
  },
  {
    files: ["*.config.{js,ts}"],
    languageOptions: { globals: { ...globals.node } },
  },
  prettier,
);
