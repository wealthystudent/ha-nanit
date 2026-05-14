import resolve from "@rollup/plugin-node-resolve";
import terser from "@rollup/plugin-terser";
import typescript from "@rollup/plugin-typescript";

export default {
  input: "src/nanit-card.ts",
  output: {
    file: "../custom_components/nanit/frontend/nanit-card.js",
    format: "es",
    sourcemap: false,
  },
  plugins: [
    typescript(),
    resolve(),
    terser({
      format: { comments: false },
    }),
  ],
};
