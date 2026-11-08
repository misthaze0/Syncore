import { confirmAction } from '@/stores/confirmStore';
/**
 * 设置 → 数据：对话产生的工作区文件夹
 *
 * ★ 为什么单独一页
 * 自动分配的文件夹是「一个对话窗口一个」，聊上几十次就是几十个。它们不该挤进
 * 输入框那颗「选择工作区」胶囊（那会把用户真正点选过的项目淹没掉），
 * 但用户又必须能看见「哪个对话占了多少磁盘、能不能打开或清掉」——
 * 这一页就是那个地方。
 *
 * ★ 删除的边界
 * 只删**自动分配**的目录：它是系统建的、只装这段对话的产出。
 * 手动登记的工作区是用户自己的项目目录，撤销授权永远不碰磁盘内容
 * （判据在后端 `workdir.delete_workdir(purge=)`，前端不自己判断）。
 */

import { useCallback, useEffect, useState } from 'react';

import { Copy, FolderOpen, RefreshCw, Trash } from '@/components/icons';
import { PageHead, Section, Table, type TableColumn } from '@/components/settings2/kit';
import { deleteWorkdir, fetchAutoWorkdirs } from '@/lib/api';
import { toast } from '@/stores/toastStore';
import type { AutoWorkdirEntry, AutoWorkdirListResponse } from '@/types';
import { isDesktopShell } from '@/types/desktop';

function fmtBytes(bytes: number): string {
  if (!bytes) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value >= 10 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
}

/** ISO 时间 → `2026-10-29 14:05`（本地时区；解析失败就原样显示） */
function fmtTime(iso: string): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

const COLUMNS: TableColumn[] = [
  { key: 'name', title: '文件夹' },
  { key: 'title', title: '对话', muted: true },
  { key: 'files', title: '文件', numeric: true, width: 64 },
  { key: 'bytes', title: '占用', numeric: true, width: 88 },
  { key: 'created_at', title: '创建时间', muted: true, width: 140 },
  { key: 'actions', title: '', width: 130 },
];

export function DataPage() {
  const [data, setData] = useState<AutoWorkdirListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');
  const desktop = isDesktopShell();

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      setData(await fetchAutoWorkdirs());
    } catch (e) {
      setError(e instanceof Error ? e.message : '读取失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const openFolder = async (entry: AutoWorkdirEntry) => {
    const bridge = window.xjmsDesktop;
    if (bridge?.shell?.openPath) {
      const ok = await bridge.shell.openPath(entry.path);
      if (!ok) {
        toast('文件夹没打开（可能已经被删掉了）', 'error');
        void load();
      }
      return;
    }
    // 网页版没有「打开资源管理器」这回事，退化成复制路径
    try {
      await navigator.clipboard.writeText(entry.path);
      toast('路径已复制，去文件管理器里粘贴', 'success');
    } catch {
      toast('无法复制路径，请从列表中选择路径并手动复制', 'error');
    }
  };

  const removeFolder = async (entry: AutoWorkdirEntry) => {
    if (busy) return;
    const what = entry.files
      ? `删除「${entry.name}」里的 ${entry.files} 个文件？`
      : `删除空文件夹「${entry.name}」？`;
    const ok = await confirmAction({
      title: what,
      description: '此操作将永久删除文件夹及其内容，无法撤销。这个对话的后续产出会放进一个新文件夹。',
    });
    if (!ok) return;
    setBusy(entry.id);
    try {
      await deleteWorkdir(entry.id, { purge: true });
      toast('文件夹已删除', 'success');
      await load();
    } catch (e) {
      toast(e instanceof Error ? e.message : '删除失败', 'error');
    } finally {
      setBusy('');
    }
  };

  const items = data?.workdirs ?? [];

  return (
    <div className="tset tset-page tset-page--wide">
      <PageHead title="数据" />

      {error && <div className="alert alert--error">{error}</div>}

      <div className="set-filters">
        <div className="set-filters__meta">
          共 {data?.total ?? 0} 个 · {data?.files ?? 0} 个文件 · {fmtBytes(data?.bytes ?? 0)}
        </div>
        <div className="set-filters__right">
          <button type="button" className="btn btn--sm" onClick={() => void load()} disabled={loading}>
            {loading ? <span className="spinner" /> : <RefreshCw size={13} />} 刷新
          </button>
        </div>
      </div>

      <Section title="对话工作区">
        <Table
          columns={COLUMNS}
          rows={items}
          rowKey={(row) => row.id}
          empty={
            <div className="empty-state">
              <span className="empty-state__icon">
                <FolderOpen size={24} />
              </span>
              <p className="empty-state__title">暂无文件夹</p>
            </div>
          }
          render={(row, col) => {
            switch (col.key) {
              case 'name':
                return (
                  <span className="data-name" title={row.path}>
                    {row.name}
                    {!row.exists && <span className="data-name__gone"> 已不在</span>}
                  </span>
                );
              case 'title':
                return row.title || (row.thread_id ? `${row.thread_id.slice(0, 8)}…` : '—');
              case 'files':
                return row.files;
              case 'bytes':
                return fmtBytes(row.bytes);
              case 'created_at':
                return fmtTime(row.created_at);
              case 'actions':
                return (
                  <span className="data-actions">
                    <button
                      type="button"
                      className="btn btn--sm"
                      onClick={() => void openFolder(row)}
                      disabled={!row.exists}
                      title={desktop ? '在资源管理器里打开' : `复制路径：${row.path}`}
                    >
                      {desktop ? <FolderOpen size={13} /> : <Copy size={13} />}
                      {desktop ? '打开' : '复制'}
                    </button>
                    <button
                      type="button"
                      className="btn btn--sm btn--danger"
                      onClick={() => void removeFolder(row)}
                      disabled={busy === row.id}
                      title="删除这个文件夹及其内容"
                    >
                      {busy === row.id ? <span className="spinner" /> : <Trash size={13} />}
                    </button>
                  </span>
                );
              default:
                return null;
            }
          }}
        />
      </Section>

      <p className="tset-page__desc data-foot">
        文件夹都建在：<code>{data?.root || '—'}</code>
      </p>
    </div>
  );
}
