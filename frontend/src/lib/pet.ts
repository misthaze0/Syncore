import type { PetState } from '@/types/desktop';

export const DEFAULT_PET_STATE: PetState = {
  enabled: false, x: null, y: null, character: 'heiress',
  alwaysOnTop: 'floating', idleTimeoutMs: 300000, muteTts: true,
  scale: 1, quietMode: false, reducedMotion: false,
  wakeEnabled: false, wakeWord: '你好，助手', wakeAck: '我在',
  remindersEnabled: false, reminderMinutes: 45, focusMinutes: 25,
  focusEndsAt: null, focusRemainingMs: null,
};

export const PET_ART = `${import.meta.env.BASE_URL}assistant-pet.png`;

export function formatPetTime(ms: number) {
  const seconds = Math.max(0, Math.ceil(ms / 1000));
  return `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
}
