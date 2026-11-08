import { confirmAction } from '@/stores/confirmStore';
/**
 * 按项目隔离知识库（ADR 0022）的编辑区。
 *
 * ★ 文案口径是这里的重点，不是装饰
 *
 * 这一层做的是**取用隔离**：越界的资料检索不到。但向量仍然写在同一个 Milvus
 * collection 里 —— 直连数据库照样拿得到，索引侧也没有按项目分库。
 * 所以界面只许说「该员工仅可检索本项目资料」，说「数据已按项目隔离」是撒谎，
 * 而且撒的是一个安全边界的谎。
 *
 * ★ 为什么规则写成「没声明 = 公共」
 *
 * 反过来（没声明 = 看不见）会让用户今天这套全局知识库在开启隔离的那一刻
 * 对所有员工同时失效，症状是「它突然不认识我的资料了」。
 * 所以这里默认全可见，声明是把范围**收窄**，不是把范围**建立**。
 *
 * ★ 前缀的形状要和索引侧同源
 *
 * 边界写的是 `source` 前缀，即 `<监听目录名>/<相对路径>`（POSIX 分隔）。
 * 用户在这里填一个绝对路径（`D:/知识/项目A`）永远不会命中，而且不报错 ——
 * 所以下方用真实入库的 source 给了可复制的例子。
 */

import { useCallback, useEffect, useMemo, useState } from 'react';

import { Pencil, RefreshCw, Trash2, X } from '@/components/icons';
import {
  deleteKnowledgeScope,
  fetchKnowledgeScope,
  fetchWorkspaces,
  putKnowledgeScope,
} from '@/lib/api';
import type { IndexDocument, KnowledgeBoundary, KnowledgeScopeResponse, Workspace } from '@/types';
import { Row, Rows, Section } from './kit';

/** 一行一条前缀的文本框 ↔ 数组（空行与首尾空白在提交时丢掉） */
function toLines(prefixes: string[]): string {
  return prefixes.join('\n');
}

function fromLines(text: string): string[] {
  return text
    .split('\n')
    .map((s) => s.trim().replace(/\\/g, '/').replace(/^\/+|\/+$/g, ''))
    .filter(Boolean);
}

interface Draft {
  workspaceId: string;
  text: string;
}

export function KnowledgeScopeEditor({ docs }: { docs: IndexDocument[] | null }) {
  const [scope, setScope] = useState<KnowledgeScopeResponse | null>(null);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [s, w] = await Promise.all([fetchKnowledgeScope(), fetchWorkspaces()]);
      setScope(s);
      setWorkspaces(w.workspaces);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const nameOf = useCallback(
    (id: string) => workspaces.find((w) => w.id === id)?.name ?? id,
    [workspaces],
  );

  /** 还没声明过的项目才出现在「新增」下拉里 */
  const unclaimed = useMemo(() => {
    const declared = new Set((scope?.boundaries ?? []).map((b) => b.workspace_id));
    return workspaces.filter((w) => !declared.has(w.id));
  }, [scope, workspaces]);

  const onSave = useCallback(
    async (draft: Draft) => {
      setBusy(draft.workspaceId);
      setError(null);
      try {
        await putKnowledgeScope(draft.workspaceId, fromLines(draft.text));
        setDraft(null);
        await load();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(null);
      }
    },
    [load],
  );

  const onDrop = useCallback(
    async (b: KnowledgeBoundary) => {
      if (
        !await confirmAction({ title: '移除项目检索范围？', description: `移除「${nameOf(b.workspace_id)}」的范围声明后，该项目的员工将能检索整个知识库。`, confirmLabel: '移除范围', tone: 'warning' })
      )
        return;
      setBusy(b.workspace_id);
      setError(null);
      try {
        await deleteKnowledgeScope(b.workspace_id);
        if (draft?.workspaceId === b.workspace_id) setDraft(null);
        await load();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(null);
      }
    },
    [draft, load, nameOf],
  );

  const declared = scope?.boundaries ?? [];

  return (
    <Section title="项目检索范围">
      {error && <div className="alert alert--error">{error}</div>}

      {!scope ? (
        error ? <button type="button" className="btn btn--sm btn--ghost" onClick={() => void load()}>重新读取项目范围</button> : <div className="alert alert--info">
          <span className="spinner" /> 正在读取项目边界…
        </div>
      ) : (
        <>
          <div className="stat-strip">
            <span>
              已声明项目 <b>{scope.workspaces_declared}</b>
            </span>
            <span title="后端开关：关掉时所有声明都不参与检索过滤">
              检索收窄 <b>{scope.enabled ? '生效中' : '未启用'}</b>
            </span>
          </div>

          <div className="console__toolbar">
            <button
              type="button"
              className="btn btn--sm btn--ghost"
              onClick={() => void load()}
              disabled={busy !== null}
            >
              <RefreshCw size={13} /> 刷新
            </button>
          </div>

          {!scope.enabled && declared.length === 0 && (
            <div className="empty-hint">暂无项目声明</div>
          )}

          {declared.length > 0 && (
            <Rows>
              {declared.map((b) => (
                <Row
                  key={b.workspace_id}
                  label={
                    <span className="clamp1" style={{ display: 'block' }} title={b.workspace_id}>
                      {b.workspace_name || b.workspace_id}
                    </span>
                  }
                  desc={b.prefixes.join('、') || '没填范围'}
                  title={
                    b.prefixes.length === 0
                      ? '声明为空等于没有收窄：这个员工仍能看到整个知识库'
                      : undefined
                  }
                  control={
                    <>
                      {!b.workspace_exists && (
                        <span
                          className="badge badge--danger"
                          title="项目已经删除，但这条声明还在把命中的资料判给一个不存在的项目"
                        >
                          项目已删除
                        </span>
                      )}
                      {draft?.workspaceId === b.workspace_id ? (
                        <span className="badge badge--accent">正在编辑</span>
                      ) : (
                        <>
                          <button
                            type="button"
                            className="btn btn--sm btn--ghost"
                            onClick={() =>
                              setDraft({
                                workspaceId: b.workspace_id,
                                text: toLines(b.prefixes),
                              })
                            }
                            title="编辑该项目的前缀"
                            aria-label={`编辑 ${b.workspace_name || b.workspace_id}`}
                          >
                            <Pencil size={13} />
                          </button>
                          <button
                            type="button"
                            className="btn btn--sm btn--ghost"
                            onClick={() => void onDrop(b)}
                            disabled={busy === b.workspace_id}
                            title="撤掉声明"
                            aria-label={`撤掉 ${b.workspace_name || b.workspace_id} 的声明`}
                          >
                            {busy === b.workspace_id ? <span className="spinner" /> : <Trash2 size={13} />}
                          </button>
                        </>
                      )}
                    </>
                  }
                />
              ))}
            </Rows>
          )}

          {!draft && unclaimed.length > 0 && (
            <Rows>
              <Row
                label="为项目声明范围"
                control={
                  <select
                    className="select"
                    value=""
                    onChange={(e) =>
                      setDraft(e.target.value ? { workspaceId: e.target.value, text: '' } : null)
                    }
                    aria-label="选择要声明范围的项目"
                    title="只能选还没声明过的项目：一个项目只有一份声明，改范围请先在列表里编辑"
                  >
                    <option value="">选择项目…</option>
                    {unclaimed.map((w) => (
                      <option key={w.id} value={w.id}>
                        {w.name}
                      </option>
                    ))}
                  </select>
                }
              />
            </Rows>
          )}

          {draft && (
            <Rows>
              <Row
                stack
                label={`「${nameOf(draft.workspaceId)}」的资料范围`}
                control={
                  <>
                    <textarea
                      className="persona-form__area"
                      rows={Math.min(10, Math.max(3, draft.text.split('\n').length + 1))}
                      value={draft.text}
                      onChange={(e) => setDraft({ ...draft, text: e.target.value })}
                      placeholder={'项目A/需求文档'}
                      spellCheck={false}
                      title="一行一条来源前缀，保存即整体替换"
                    />
                    {docs && docs.length > 0 && (
                      <div className="ksc-sources">
                        {docs.slice(0, 8).map((d) => (
                          <button
                            type="button"
                            key={d.source}
                            className="ksc-src"
                            onClick={() =>
                              setDraft({
                                ...draft,
                                text: draft.text.trim() ? `${draft.text.replace(/\s+$/, '')}\n${d.source}` : d.source,
                              })
                            }
                          >
                            {d.source}
                          </button>
                        ))}
                        {docs.length > 8 && (
                          <span className="ksc-src-more">共 {docs.length} 个来源</span>
                        )}
                      </div>
                    )}
                    <div className="console__toolbar" style={{ marginTop: 8 }}>
                      <button
                        type="button"
                        className="btn btn--sm btn--primary"
                        onClick={() => void onSave(draft)}
                        disabled={busy === draft.workspaceId}
                      >
                        {busy === draft.workspaceId ? <span className="spinner" /> : '保存'}
                      </button>
                      <button
                        type="button"
                        className="btn btn--sm btn--ghost"
                        onClick={() => setDraft(null)}
                      >
                        <X size={13} /> 取消
                      </button>
                    </div>
                  </>
                }
              />
            </Rows>
          )}
        </>
      )}
    </Section>
  );
}

export default KnowledgeScopeEditor;
