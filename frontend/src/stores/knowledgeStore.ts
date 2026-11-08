import { create } from 'zustand';
import { uploadKnowledgeFile } from '@/lib/knowledgeApi';

export const KNOWLEDGE_EXTENSIONS = ['.pdf', '.docx', '.pptx', '.xlsx', '.md', '.markdown', '.txt', '.csv', '.log', '.py', '.js', '.ts', '.json', '.yaml', '.yml'];
export interface KnowledgeUpload {
  id: string;
  name: string;
  size: number;
  file?: File;
  state: 'queued' | 'uploading' | 'indexed' | 'warning' | 'error';
  message: string;
  chunks?: number;
}
interface KnowledgeStore {
  uploads: KnowledgeUpload[];
  uploading: boolean;
  revision: number;
  enqueue: (files: FileList | File[], maxSizeMb?: number, extensions?: string[]) => void;
  retry: (id: string) => void;
  dismiss: (id: string) => void;
  clearFinished: () => void;
}

// A separate store keeps knowledge ingestion visible across page changes.
// Completed uploads release their File objects; only failed uploads retain a retry copy.
export const useKnowledgeStore = create<KnowledgeStore>((set, get) => ({
  uploads: [], uploading: false, revision: 0,
  enqueue: (files, maxSizeMb = 50, extensions = KNOWLEDGE_EXTENSIONS) => {
    const items = Array.from(files).map((file): KnowledgeUpload => {
      const extension = `.${file.name.split('.').pop()?.toLowerCase()}`;
      const error = !extensions.includes(extension) ? '不支持这个格式，请转换成受支持的文件后上传'
        : file.size > maxSizeMb * 1024 * 1024 ? `文件超过 ${maxSizeMb} MB 上限` : '';
      return { id: crypto.randomUUID(), name: file.name, size: file.size, file: error ? undefined : file,
        state: error ? 'error' : 'queued', message: error || '等待入库' };
    });
    set((state) => ({ uploads: [...state.uploads.filter(x => x.state === 'queued' || x.state === 'uploading' || x.state === 'error'),
      ...state.uploads.filter(x => x.state === 'indexed' || x.state === 'warning').slice(-20), ...items] }));
    void drain();
  },
  retry: (id) => {
    set({ uploads: get().uploads.map(item => item.id === id && item.file && item.state === 'error'
      ? { ...item, state: 'queued', message: '等待重试' } : item) });
    void drain();
  },
  dismiss: (id) => set({ uploads: get().uploads.filter(item => item.id !== id || item.state === 'uploading') }),
  clearFinished: () => set({ uploads: get().uploads.filter(item => item.state === 'queued' || item.state === 'uploading' || item.state === 'error') }),
}));

let draining = false;
async function drain(): Promise<void> {
  if (draining) return;
  draining = true;
  const { setState, getState } = useKnowledgeStore;
  setState({ uploading: true });
  try {
    while (true) {
      const next = getState().uploads.find(item => item.state === 'queued');
      if (!next) break;
      setState(state => ({ uploads: state.uploads.map(item => item.id === next.id
        ? { ...item, state: 'uploading', message: '正在上传、解析与建立索引…' } : item) }));
      try {
        if (!next.file) throw new Error('请重新选择文件');
        const response = await uploadKnowledgeFile(next.file);
        setState(state => ({ revision: state.revision + 1, uploads: state.uploads.map(item => item.id === next.id
          ? { ...item, file: undefined, state: response.warnings.length ? 'warning' : 'indexed',
            message: response.message, chunks: response.chunks_indexed } : item) }));
      } catch (error) {
        setState(state => ({ revision: state.revision + 1, uploads: state.uploads.map(item => item.id === next.id
          ? { ...item, state: 'error', message: error instanceof Error ? error.message : String(error) } : item) }));
      }
    }
  } finally {
    draining = false;
    setState({ uploading: false });
  }
}
