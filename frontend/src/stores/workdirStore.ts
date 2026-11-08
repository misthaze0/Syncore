/**
 * 工作区（用户授权的本地目录）的前端状态
 *
 * ★ 真相在后端，这里只是它的一个视图
 *
 * 「哪个会话正在用哪个目录」存在服务端的绑定表里（见 `app/workdir`）。
 * 这份 store 不写 localStorage —— 如果本地也存一份，就会有「界面显示着 A 项目，
 * 后端其实跑在沙箱里」这种两边都觉得自己没错的分裂。
 * 代价是切会话要发一次请求；换来的是换设备、换标签页看到的都是同一个事实。
 *
 * ★ 为什么按会话而不是全局一个当前工作区
 * 办公类产品同时开着几段对话是常态：一段在弄量化项目，一段在整理周报。
 * 全局当前值意味着后者会悄悄改掉了前者的文件边界。
 */

import { create } from 'zustand';

import {
  createWorkdir,
  deleteWorkdir,
  fetchWorkdirBinding,
  fetchWorkdirs,
  setWorkdirBinding,
} from '@/lib/api';
import type { WorkdirBinding, WorkdirEntry } from '@/types';

interface WorkdirStore {
  /** 全部已登记的工作区（按最近使用排序；只有手动登记的，自动目录在「设置 → 数据」） */
  workdirs: WorkdirEntry[];
  /** 后端认可的图标名清单（由接口给出，前端不抄一份） */
  icons: string[];
  /** 清单是否已经拉过（第一次打开选择器时才拉，不进主界面就发请求） */
  loaded: boolean;
  /** 正在加载清单（选择器据此显示骨架而不是一片空白） */
  loading: boolean;
  /** 当前会话绑定的工作区 id（空串 = 不指定，走沙箱；`auto` = 交给系统分配） */
  boundId: string;
  /** 绑定属于哪一类 —— 界面据此决定胶囊显示「自动」还是目录名 */
  boundMode: WorkdirBinding['mode'];
  /**
   * 绑定的那个目录条目。
   *
   * 不能只在 `workdirs` 里找：自动分配出来的目录不在那份清单里，
   * 光靠 id 反查会在首条消息之后把胶囊变回「未指定」。
   */
  boundEntry: WorkdirEntry | null;
  /** `boundId` 属于哪个会话 —— 换会话时用它挡住还在飞的旧请求 */
  boundThreadId: string;
  /** 刚创建完、选择器应当立刻把新建项顶上来的那条（用于高亮，不参与排序） */
  lastCreated: WorkdirEntry | null;

  load: (force?: boolean) => Promise<void>;
  /** 读某个会话的绑定。切会话时调用。 */
  syncBinding: (threadId: string) => Promise<void>;
  /** 绑定 / 解绑（`workdirId` 空串 = 不指定；`auto` = 由系统分配）。失败时抛错给界面提示。 */
  bind: (threadId: string, workdirId: string) => Promise<void>;
  create: (payload: { path: string; name?: string; icon?: string; color?: string }) => Promise<WorkdirEntry>;
  remove: (id: string) => Promise<void>;
}

let inflight: Promise<void> | null = null;

export const useWorkdirStore = create<WorkdirStore>((set, get) => ({
  workdirs: [],
  icons: [],
  loaded: false,
  loading: false,
  boundId: '',
  boundMode: 'none',
  boundEntry: null,
  boundThreadId: '',
  lastCreated: null,

  load: async (force = false) => {
    if (get().loaded && !force) return;
    // 连点两下选择器会发两次请求；共用同一个 in-flight 就够了，不值得上缓存层
    if (inflight) return inflight;
    set({ loading: true });
    inflight = fetchWorkdirs()
      .then((res) => set({ workdirs: res.workdirs, icons: res.icons, loaded: true }))
      .catch((e) => {
        // 拉不到清单不弹错 —— 选择器自己会显示「一个工作区也没有」，
        // 那比「在用户点开之前就先报一次错」更贴近他真正关心的事。
        console.warn('工作区清单加载失败', e);
      })
      .finally(() => {
        inflight = null;
        set({ loading: false });
      });
    return inflight;
  },

  syncBinding: async (threadId) => {
    // 先占位再发请求：回来的时候只认这一个 threadId，
    // 上一段会话那个还没落地的绑定就会被这里挡掉。
    set({ boundThreadId: threadId, boundId: '', boundMode: 'none', boundEntry: null });
    if (!threadId) return;
    try {
      const res = await fetchWorkdirBinding(threadId);
      if (get().boundThreadId !== threadId) return;
      set({ boundId: res.workdir_id, boundMode: res.mode, boundEntry: res.workdir });
    } catch (e) {
      console.warn('工作区绑定读取失败', e);
    }
  },

  bind: async (threadId, workdirId) => {
    const res = await setWorkdirBinding(threadId, workdirId);
    set({
      boundId: res.workdir_id,
      boundMode: res.mode,
      boundEntry: res.workdir,
      boundThreadId: threadId,
    });
    // 顺序与「最近使用」都变了，但这是用户马上要再看一眼的那个列表 ——
    // 静默重拉比让他看到一个过期顺序好。
    void get().load(true);
  },

  create: async (payload) => {
    const entry = await createWorkdir(payload);
    set((s) => ({
      workdirs: [entry, ...s.workdirs.filter((w) => w.id !== entry.id)],
      lastCreated: entry,
    }));
    return entry;
  },

  remove: async (id) => {
    await deleteWorkdir(id);
    set((s) => ({
      workdirs: s.workdirs.filter((w) => w.id !== id),
      lastCreated: s.lastCreated?.id === id ? null : s.lastCreated,
    }));
    // 后端删工作区时会连带清掉引用它的绑定，所以这里要重新读一次当前会话的绑定，
    // 而不是在前端猜「它是不是正好是被删的那个」。
    if (get().boundId === id) {
      set({ boundId: '', boundMode: 'none', boundEntry: null });
      await get().syncBinding(get().boundThreadId);
    }
  },
}));
