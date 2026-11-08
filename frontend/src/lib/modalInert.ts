const locks = new WeakMap<HTMLElement, { count: number; previous: boolean }>();

/** Multiple overlays may release the same background in a different order. */
export function acquireModalInert(element: HTMLElement): () => void {
  const lock = locks.get(element) ?? { count: 0, previous: element.inert };
  lock.count++;
  locks.set(element, lock);
  element.inert = true;
  let released = false;
  return () => {
    if (released) return;
    released = true;
    lock.count--;
    if (lock.count === 0) {
      element.inert = lock.previous;
      locks.delete(element);
    }
  };
}
