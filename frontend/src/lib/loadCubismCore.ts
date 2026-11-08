/**
 * 动态脚本加载器 —— Live2D Cubism Core SDK 不能 npm 安装（官方只发二进制 .js），
 * 必须用 <script> 标签挂在全局上才能让 pixi-live2d-display 调到 `Live2DCubismCore`。
 *
 * 不能在 HTML 里写死 <script src="/live2d-core/...">：桌面端 file:// 加载时
 * 绝对路径会解析到磁盘根；base='./' 又只作用于 vite 打包的资源，对 public/ 里的
 * 裸 <script> 不生效。运行时动态拼相对路径最稳。
 */

const CUBISM_CORE_URL = './live2d-core/live2dcubismcore.min.js';

let loaderPromise: Promise<void> | null = null;

/** 等待全局 `Live2DCubismCore` 出现；已加载过就直接 resolve */
export function ensureCubismCore(): Promise<void> {
  if (typeof window !== 'undefined' && (window as any).Live2DCubismCore) {
    return Promise.resolve();
  }
  if (loaderPromise) return loaderPromise;

  loaderPromise = new Promise<void>((resolve, reject) => {
    const script = document.createElement('script');
    script.src = CUBISM_CORE_URL;
    script.async = true;
    script.onload = () => {
      // Cubism Core 加载后会在 window 上挂 Live2DCubismCore
      if ((window as any).Live2DCubismCore) {
        resolve();
      } else {
        reject(new Error('Cubism Core 脚本已加载但全局 Live2DCubismCore 未挂载'));
      }
    };
    script.onerror = () => reject(new Error(`加载 Cubism Core 失败：${CUBISM_CORE_URL}`));
    document.head.appendChild(script);
  });

  return loaderPromise;
}
