import { useCallback, useEffect, useRef, useState } from 'react';
import { StreamingSpeechPlayer } from '@/lib/ttsPlayback';

export interface TTSPlayerCallbacks {
  onSpeak?: (text: string) => void;
  onIdle?: () => void;
  onError?: (message: string) => void;
}

/** Stream audio immediately, with one sentence of synthesis lookahead. */
export function useTTSPlayer(muted: boolean, modelRef: React.MutableRefObject<any | null>, callbacks: TTSPlayerCallbacks) {
  const [isPlaying, setIsPlaying] = useState(false);
  const callbacksRef = useRef(callbacks);
  callbacksRef.current = callbacks;
  const mutedRef = useRef(muted);
  mutedRef.current = muted;
  const player = useRef<StreamingSpeechPlayer | null>(null);
  const getPlayer = useCallback(() => {
    player.current ??= new StreamingSpeechPlayer({
      request: async (text, signal) => {
        const base = import.meta.env.VITE_API_BASE_URL ?? '/api';
        const options = { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text }), signal };
        const response = await fetch(base + '/voice/tts/stream', options);
        // Allow the desktop renderer to work during a rolling backend restart.
        return response.status === 404 || response.status === 405 ? fetch(base + '/voice/tts', options) : response;
      },
      onBusy: setIsPlaying,
      onSpeak: (text) => callbacksRef.current.onSpeak?.(text),
      onIdle: () => callbacksRef.current.onIdle?.(),
      onError: (message) => callbacksRef.current.onError?.(message),
      onLevel: (level) => modelRef.current?.internalModel?.coreModel?.setParameterValueById?.('ParamMouthOpenY', level),
    });
    return player.current;
  }, [modelRef]);
  const speak = useCallback((text: string) => { if (!mutedRef.current) getPlayer().speak(text); }, [getPlayer]);
  const warm = useCallback((text: string) => mutedRef.current ? () => {} : getPlayer().warm(text), [getPlayer]);
  const stop = useCallback(() => player.current?.stop(), []);
  useEffect(() => { if (muted) stop(); }, [muted, stop]);
  useEffect(() => () => { player.current?.close(); player.current = null; }, []);
  return { speak, stop, warm, isPlaying };
}
