// Persisted preferences are also IPC input: only accept known, bounded values.
const DEFAULT_PET_STATE = Object.freeze({
  enabled: false, x: null, y: null, character: 'heiress',
  alwaysOnTop: 'floating', idleTimeoutMs: 300000, muteTts: true,
  scale: 1, quietMode: false, reducedMotion: false,
  wakeEnabled: false, wakeWord: '你好，助手', wakeAck: '我在',
  remindersEnabled: false, reminderMinutes: 45, focusMinutes: 25,
  focusEndsAt: null, focusRemainingMs: null,
});

function normalizePetState(input) {
  const next = { ...DEFAULT_PET_STATE };
  if (!input || typeof input !== 'object' || Array.isArray(input)) return next;
  for (const key of ['enabled', 'muteTts', 'quietMode', 'reducedMotion', 'remindersEnabled', 'wakeEnabled']) {
    if (typeof input[key] === 'boolean') next[key] = input[key];
  }
  if (typeof input.wakeWord === 'string') {
    const word = input.wakeWord.trim();
    if (word && word.length <= 32) next.wakeWord = word;
  }
  if (typeof input.wakeAck === 'string') {
    const ack = input.wakeAck.trim();
    if (ack && ack.length <= 50) next.wakeAck = ack;
  }
  for (const key of ['x', 'y']) {
    if (Number.isFinite(input[key]) && Math.abs(input[key]) <= 100000) next[key] = Math.round(input[key]);
  }
  if (['floating', 'screen-saver', 'normal'].includes(input.alwaysOnTop)) next.alwaysOnTop = input.alwaysOnTop;
  const ranges = { scale: [0.75, 1.25], idleTimeoutMs: [30000, 3600000], reminderMinutes: [15, 120], focusMinutes: [1, 120] };
  for (const [key, [min, max]] of Object.entries(ranges)) {
    if (Number.isFinite(input[key])) next[key] = Math.max(min, Math.min(max, input[key]));
  }
  if (Number.isFinite(input.focusEndsAt) && input.focusEndsAt > 0 && input.focusEndsAt <= 8640000000000000) next.focusEndsAt = input.focusEndsAt;
  if (Number.isFinite(input.focusRemainingMs) && input.focusRemainingMs > 0) next.focusRemainingMs = Math.min(7200000, input.focusRemainingMs);
  // A running deadline wins over a stale paused duration.
  if (next.focusEndsAt !== null) next.focusRemainingMs = null;
  return next;
}

function petSize(scale = 1) {
  return { width: Math.round(340 * scale), height: Math.round(500 * scale) };
}

function clampPetBounds(bounds, workArea) {
  return { ...bounds,
    x: Math.round(Math.max(workArea.x, Math.min(workArea.x + Math.max(0, workArea.width - bounds.width), bounds.x))),
    y: Math.round(Math.max(workArea.y, Math.min(workArea.y + Math.max(0, workArea.height - bounds.height), bounds.y))),
  };
}

module.exports = { DEFAULT_PET_STATE, normalizePetState, petSize, clampPetBounds };
