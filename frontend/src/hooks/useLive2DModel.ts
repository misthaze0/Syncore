import { useCallback, useEffect, useRef, useState } from 'react';
import { ensureCubismCore } from '@/lib/loadCubismCore';
import { petInteractionBlocked, samplePetGesture, type PetGesture } from '@/lib/petAnimation';

export type Live2DStatus = 'loading' | 'ready' | 'error';
export interface PetRigControls { mood: string; reducedMotion: boolean; dragging: boolean; gesture: PetGesture | null }
/** Shared by the notification and voice-turn players; audio owns the mouth. */
export interface PetMouthModel {
  internalModel: { coreModel: { setParameterValueById: (id: string, value: number) => void } };
}

const MODEL_URL = `${import.meta.env.BASE_URL}models/miss-jiu/MissJiu.model3.json`;
const clamp = (value: number, min: number, max: number) => Math.max(min, Math.min(max, value));

export function useLive2DModel(
  modelRef: React.MutableRefObject<PetMouthModel | null>,
  controls: React.MutableRefObject<PetRigControls>,
) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [status, setStatus] = useState<Live2DStatus>('loading');
  const [error, setError] = useState<string | null>(null);
  const pixelHit = useRef<((x: number, y: number) => boolean) | null>(null);
  const hitTest = useCallback((x: number, y: number) => pixelHit.current?.(x, y) ?? false, []);

  useEffect(() => {
    let cancelled = false;
    let app: import('pixi.js').Application | null = null;
    let model: any = null;
    let resizeObserver: ResizeObserver | null = null;
    let mouth = 0;
    let elapsed = 0;
    let blinkAt = 2600;
    let blinkStart = -1;
    let pointerPoll: number | undefined;
    let pointerPending = false;
    let frameMs = 33;
    const smoothPose = new Map<string, number>();
    const pointer = { x: 0, y: 0, targetX: 0, targetY: 0 };
    const media = window.matchMedia('(prefers-reduced-motion: reduce)');
    const aim = (clientX: number, clientY: number) => {
      const bounds = canvasRef.current?.getBoundingClientRect();
      if (!bounds) return;
      pointer.targetX = clamp((clientX - bounds.left - bounds.width / 2) / (bounds.width * .8), -1, 1);
      pointer.targetY = clamp((clientY - bounds.top - bounds.height * .16) / (bounds.height * .7), -1, 1);
    };
    const move = (event: MouseEvent) => aim(event.clientX, event.clientY);
    const leave = () => { pointer.targetX = 0; pointer.targetY = 0; };
    const visibility = () => {
      if (document.hidden) app?.stop();
      else app?.start();
    };
    const contextLost = (event: Event) => {
      event.preventDefault();
      app?.stop();
      modelRef.current = null;
      pixelHit.current = null;
      if (!cancelled) { setError('图形环境暂时不可用'); setStatus('error'); }
    };

    async function load() {
      try {
        await ensureCubismCore();
        if (cancelled) return;
        const [PIXI, live2d] = await Promise.all([import('pixi.js'), import('pixi-live2d-display/cubism4')]);
        if (cancelled) return;
        const canvas = canvasRef.current;
        if (!canvas) return;
        live2d.Live2DModel.registerTicker(PIXI.Ticker);
        app = new PIXI.Application({
          view: canvas, width: canvas.clientWidth || 284, height: canvas.clientHeight || 378,
          resolution: Math.min(window.devicePixelRatio || 1, 2), autoDensity: true,
          backgroundAlpha: 0, antialias: true, preserveDrawingBuffer: true, autoStart: false,
        });
        app.ticker.maxFPS = 30;
        model = await live2d.Live2DModel.from(MODEL_URL, { autoInteract: false, autoUpdate: false });
        if (cancelled) { model.destroy(); return; }
        app.stage.addChild(model);
        model.anchor.set(.5, .5);
        const originalHeight = model.internalModel.originalHeight;
        const fit = () => {
          if (!app || !canvasRef.current) return;
          const bounds = canvasRef.current.getBoundingClientRect();
          app.renderer.resize(bounds.width, bounds.height);
          model.scale.set(bounds.height / originalHeight);
          model.position.set(bounds.width / 2, bounds.height / 2);
        };
        fit();
        resizeObserver = new ResizeObserver(fit);
        resizeObserver.observe(canvas);
        const core = model.internalModel.coreModel;
        // Clear any initial clip. The shared driver applies all controlled
        // parameters immediately before rendering, including audio-owned lips.
        model.internalModel.motionManager.stopAllMotions();
        modelRef.current = { internalModel: { coreModel: {
          setParameterValueById: (id, value) => {
            if (id === 'ParamMouthOpenY') mouth = clamp(Number.isFinite(value) ? value : 0, 0, .85);
            else core.setParameterValueById(id, value);
          },
        } } };

        model.internalModel.on('beforeModelUpdate', () => {
          const current = controls.current;
          const still = current.reducedMotion || media.matches;
          const sleeping = current.mood === 'sleeping';
          const time = elapsed / 1000;
          const blend = 1 - Math.exp(-frameMs / 130);
          pointer.x += ((still ? 0 : pointer.targetX) - pointer.x) * blend;
          pointer.y += ((still ? 0 : pointer.targetY) - pointer.y) * blend;
          let eyeOpen = 1;
          if (!still && !sleeping) {
            if (elapsed >= blinkAt && blinkStart < 0) blinkStart = elapsed;
            if (blinkStart >= 0) {
              const phase = (elapsed - blinkStart) / 280;
              eyeOpen = phase >= 1 ? 1 : Math.max(0, 1 - Math.sin(phase * Math.PI) ** 2);
              if (phase >= 1) { blinkStart = -1; blinkAt = elapsed + 3200 + Math.random() * 2200; }
            }
          }
          if (sleeping) eyeOpen = 0;
          const happy = current.mood === 'happy';
          const thinking = current.mood === 'thinking';
          const listening = current.mood === 'listening';
          const approval = current.mood === 'approval';
          const failed = current.mood === 'error';
          const gesture = !current.dragging && !sleeping && !petInteractionBlocked(current.mood) ? current.gesture : null;
          if (still && gesture?.name === 'rest') eyeOpen = 0;
          const pose = samplePetGesture(still ? null : gesture, performance.now());
          const attention = 1 - pose.attention;
          const set = (id: string, value: number) => core.setParameterValueById(id, value);
          const ease = (id: string, target: number) => {
            const previous = smoothPose.get(id) ?? target;
            const value = still ? target : previous + (target - previous) * blend;
            smoothPose.set(id, value);
            set(id, value);
          };
          // Small ranges keep the original face and neckline intact. Reactions
          // temporarily center her attention, then smoothly return to the cursor.
          ease('ParamAngleX', still ? 0 : clamp(pointer.x * 7 * attention + pose.angleX, -8, 8));
          ease('ParamAngleY', still ? 0 : clamp(-pointer.y * 4 * attention + Math.sin(time * .7) * .35 + pose.angleY, -4.5, 4.5));
          ease('ParamAngleZ', still ? 0 : clamp((thinking ? 1.1 : listening ? -1 : failed ? -.8 : Math.sin(time * .45) * .3) + pose.angleZ, -3, 3));
          ease('ParamEyeBallX', still || sleeping ? 0 : clamp(pointer.x * .35 * attention + pose.gazeX, -.4, .4));
          ease('ParamEyeBallY', still || sleeping ? 0 : clamp(-pointer.y * .25 * attention + pose.gazeY, -.3, .3));
          set('ParamEyeBallForm', 0);
          const attentiveEyes = thinking ? .88 : current.mood === 'focused' ? .92 : 1;
          set('ParamEyeLOpen', eyeOpen * attentiveEyes * pose.eyeL);
          set('ParamEyeROpen', eyeOpen * attentiveEyes * pose.eyeR);
          ease('ParamBrowLY', clamp((happy ? .08 : thinking ? -.06 : approval ? .15 : failed ? -.12 : 0) + pose.browL, -.16, .2));
          ease('ParamBrowRY', clamp((happy ? .08 : thinking ? .08 : approval ? .15 : failed ? .04 : 0) + pose.browR, -.16, .2));
          ease('ParamMouthForm', clamp((happy || (still && gesture && gesture.name !== 'rest') ? .08 : failed ? -.05 : 0) + pose.smile, -.08, .18));
          set('ParamMouthOpenY', mouth);
          set('ParamBreath', still ? 0 : .35 + Math.sin(time * (sleeping ? .65 : 1.1)) * .25);
          ease('ParamBodyAngleX', still || current.dragging ? 0 : pointer.x * .18 * attention);
          set('ParamBodyAngleY', 0);
          ease('ParamBodyAngleZ', still || current.dragging ? 0 : clamp(Math.sin(time * .6) * .12 + pose.bodyZ, -.22, .22));
          set('ParamBodyLean', 0);
          set('ParamProportion', 0);
          // Physics still evaluates, with a bounded ambient sway on top.
          set('ParamHairFront', still ? 0 : clamp(core.getParameterValueById('ParamHairFront') + Math.sin(time * 1.2) * .018, -.18, .18));
          set('ParamHairBack', still ? 0 : clamp(core.getParameterValueById('ParamHairBack') + Math.sin(time * .9 - .4) * .018, -.18, .18));
        });

        app.ticker.add(() => {
          if (!app || cancelled) return;
          const dt = Math.min(app.ticker.deltaMS, 60);
          frameMs = dt;
          elapsed += dt;
          app.ticker.maxFPS = controls.current.reducedMotion || media.matches ? 12 : 30;
          model.update(dt);
        });
        const sample = new Uint8Array(4);
        pixelHit.current = (clientX, clientY) => {
          if (!app || !canvasRef.current) return false;
          const bounds = canvasRef.current.getBoundingClientRect();
          const x = Math.floor((clientX - bounds.left) / bounds.width * canvas.width);
          const y = Math.floor((clientY - bounds.top) / bounds.height * canvas.height);
          if (x < 0 || y < 0 || x >= canvas.width || y >= canvas.height) return false;
          try {
            const gl = (app.renderer as import('pixi.js').Renderer).gl;
            gl.readPixels(x, canvas.height - y - 1, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, sample);
            return sample[3] > 24;
          } catch { return false; }
        };
        document.addEventListener('mousemove', move);
        document.addEventListener('mouseleave', leave);
        document.addEventListener('visibilitychange', visibility);
        const getPointer = window.xjmsDesktop?.pet?.getPointer;
        if (getPointer) pointerPoll = window.setInterval(() => {
          if (cancelled || pointerPending || document.hidden || controls.current.reducedMotion || media.matches || controls.current.mood === 'sleeping') return;
          pointerPending = true;
          void getPointer().then((point) => { if (point && !cancelled) aim(point.x, point.y); }).catch(() => {}).finally(() => { pointerPending = false; });
        }, 100);
        canvas.addEventListener('webglcontextlost', contextLost);
        if (!document.hidden) app.start();
        else { model.update(1); app.renderer.render(app.stage); }
        setStatus('ready');
      } catch (failure) {
        if (!cancelled) {
          modelRef.current = null;
          setError(failure instanceof Error ? failure.message : String(failure));
          setStatus('error');
          app?.stop();
        }
      }
    }
    void load();
    return () => {
      cancelled = true;
      modelRef.current = null;
      pixelHit.current = null;
      clearInterval(pointerPoll);
      document.removeEventListener('mousemove', move);
      document.removeEventListener('mouseleave', leave);
      document.removeEventListener('visibilitychange', visibility);
      canvasRef.current?.removeEventListener('webglcontextlost', contextLost);
      resizeObserver?.disconnect();
      app?.stop();
      if (model) { try { model.destroy(); } catch { /* Already released by context loss. */ } }
      app?.destroy(false, { children: false, texture: false, baseTexture: false });
      app = null;
    };
  }, [modelRef, controls]);
  return { canvasRef, status, error, hitTest };
}
