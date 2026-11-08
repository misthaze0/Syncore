/**
 * pixi-live2d-display 的子 bundle 路径走 package.json 的 exports：
 * `pixi-live2d-display/cubism4`。types 字段指向同一份 ./types/index.d.ts
 * （全集），所以 `export * from 'pixi-live2d-display'` 等价即可。
 *
 * Vite 在严格模式解析 exports 字段时，需要 d.ts shim 把子路径对到全集类型；
 * 否则 TS 会报「Cannot find module」。
 */

declare module 'pixi-live2d-display/cubism4' {
  export * from 'pixi-live2d-display';
}
