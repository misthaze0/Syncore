/** Small-angle gestures authored for Syncore's existing rig. Mouth openness is audio-only. */
export interface PetPose {
  angleX: number; angleY: number; angleZ: number;
  gazeX: number; gazeY: number; eyeL: number; eyeR: number;
  browL: number; browR: number; smile: number;
  bodyZ: number; attention: number;
}
type Track = readonly (readonly [number, number])[];
type GestureClip = { label: string; duration: number; tracks: Partial<Record<keyof PetPose, Track>> };

export const NEUTRAL_PET_POSE: Readonly<PetPose> = {
  angleX: 0, angleY: 0, angleZ: 0, gazeX: 0, gazeY: 0,
  eyeL: 1, eyeR: 1, browL: 0, browR: 0, smile: 0, bodyZ: 0, attention: 0,
};

export const PET_GESTURES = {
  greet: { label: '待命示意', duration: 2400, tracks: {
    angleY: [[0, 0], [.2, -3], [.4, .7], [.6, -1.7], [.82, .2], [1, 0]],
    angleZ: [[0, 0], [.32, 1.8], [.7, .7], [1, 0]],
    browL: [[0, 0], [.25, .12], [.75, .08], [1, 0]],
    browR: [[0, 0], [.25, .12], [.75, .08], [1, 0]],
    smile: [[0, 0], [.2, .1], [.8, .1], [1, 0]],
    attention: [[0, 0], [.16, .8], [.8, .8], [1, 0]],
  } },
  affection: { label: '回应互动', duration: 2800, tracks: {
    angleZ: [[0, 0], [.28, 2.7], [.65, 2.7], [1, 0]],
    angleY: [[0, 0], [.32, -1.2], [.72, -.8], [1, 0]],
    eyeL: [[0, 1], [.25, .75], [.4, 0], [.57, 0], [.78, .85], [1, 1]],
    eyeR: [[0, 1], [.25, .75], [.4, 0], [.57, 0], [.78, .85], [1, 1]],
    smile: [[0, 0], [.22, .14], [.78, .14], [1, 0]],
    browL: [[0, 0], [.3, .1], [.72, .1], [1, 0]],
    browR: [[0, 0], [.3, .1], [.72, .1], [1, 0]],
    attention: [[0, 0], [.2, 1], [.78, 1], [1, 0]],
  } },
  curious: { label: '正在听取', duration: 3000, tracks: {
    angleZ: [[0, 0], [.22, -2.6], [.75, -2.6], [1, 0]],
    angleX: [[0, 0], [.3, 2], [.72, 2], [1, 0]],
    gazeX: [[0, 0], [.25, -.12], [.75, -.12], [1, 0]],
    browL: [[0, 0], [.25, .16], [.75, .16], [1, 0]],
    browR: [[0, 0], [.25, .03], [.75, .03], [1, 0]],
    attention: [[0, 0], [.18, .85], [.8, .85], [1, 0]],
  } },
  cheer: { label: '完成反馈', duration: 2600, tracks: {
    angleY: [[0, 0], [.2, -2.2], [.36, 1], [.55, -2], [.72, .6], [1, 0]],
    angleZ: [[0, 0], [.25, -1.2], [.65, 1.2], [1, 0]],
    eyeL: [[0, 1], [.3, .7], [.7, .7], [1, 1]],
    eyeR: [[0, 1], [.3, .7], [.7, .7], [1, 1]],
    smile: [[0, 0], [.2, .15], [.8, .15], [1, 0]],
    browL: [[0, 0], [.25, .12], [.75, .12], [1, 0]],
    browR: [[0, 0], [.25, .12], [.75, .12], [1, 0]],
    bodyZ: [[0, 0], [.3, -.12], [.65, .12], [1, 0]],
    attention: [[0, 0], [.16, .9], [.8, .9], [1, 0]],
  } },
  rest: { label: '稍作休息', duration: 4000, tracks: {
    angleY: [[0, 0], [.3, -2], [.75, -2], [1, 0]],
    angleZ: [[0, 0], [.3, 1.1], [.75, 1.1], [1, 0]],
    eyeL: [[0, 1], [.3, 0], [.75, 0], [1, 1]],
    eyeR: [[0, 1], [.3, 0], [.75, 0], [1, 1]],
    attention: [[0, 0], [.2, 1], [.8, 1], [1, 0]],
  } },
  settle: { label: '就绪', duration: 1200, tracks: {
    angleZ: [[0, 0], [.2, -1.8], [.52, .8], [.78, -.2], [1, 0]],
    bodyZ: [[0, 0], [.2, -.12], [.52, .06], [1, 0]],
    attention: [[0, 0], [.2, .8], [.65, .6], [1, 0]],
  } },
} as const satisfies Record<string, GestureClip>;

export type PetGestureName = keyof typeof PET_GESTURES;
export interface PetGesture { name: PetGestureName; startedAt: number }
export const PET_ACTION_CHOICES: readonly { name: PetGestureName; title: string; detail: string; text: string }[] = [
  { name: 'greet', title: '待命示意', detail: '点头示意，回到待命', text: '我在，随时可以开始。' },
  { name: 'affection', title: '回应互动', detail: '短暂回应，回到待命', text: '收到。需要我处理什么？' },
  { name: 'curious', title: '认真听取', detail: '把注意力放在当前任务', text: '请说，我在听。需要执行可以双击打开工作台。' },
  { name: 'cheer', title: '完成反馈', detail: '确认完成，回到待命', text: '这一步已完成，可以继续下一项。' },
  { name: 'rest', title: '稍作休息', detail: '放松片刻，随后继续', text: '先放松一下，回来后继续。' },
];

/** Cubic interpolation gives every gesture a quiet start and a complete return. */
export function samplePetGesture(gesture: PetGesture | null, now: number): PetPose {
  const pose = { ...NEUTRAL_PET_POSE };
  if (!gesture) return pose;
  const clip: GestureClip = PET_GESTURES[gesture.name];
  const phase = (now - gesture.startedAt) / clip.duration;
  if (phase <= 0 || phase >= 1) return pose;
  for (const key of Object.keys(clip.tracks) as (keyof PetPose)[]) {
    const track = clip.tracks[key]!;
    for (let i = 1; i < track.length; i++) {
      const [end, target] = track[i];
      if (phase > end) continue;
      const [start, initial] = track[i - 1];
      const t = (phase - start) / (end - start);
      const ease = t * t * (3 - 2 * t);
      pose[key] = initial + (target - initial) * ease;
      break;
    }
  }
  return pose;
}

export function petInteractionBlocked(mood: string) {
  return ['thinking', 'approval', 'error', 'listening', 'speaking'].includes(mood);
}
