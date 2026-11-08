import { useEffect, useState } from 'react';
import { MarkdownRenderer } from '@/components/MarkdownRenderer';
import { deleteWorkspaceMemory, fetchWorkspaceArtifacts, fetchWorkspaceMemories, saveWorkspaceMemory, workspaceArtifactUrl } from '@/lib/api';
import type { WorkspaceArtifact, WorkspaceMemory } from '@/lib/api';
import type { RunSummary } from '@/types';

export function WorkspaceRunDetails({run, revision, employeeId}: {run: RunSummary | null; revision: string; employeeId: string}) {
  const [artifacts, setArtifacts] = useState<WorkspaceArtifact[]>([]);
  const [memories, setMemories] = useState<WorkspaceMemory[]>([]);
  const [error, setError] = useState('');
  const [key, setKey] = useState('');
  const [value, setValue] = useState('');
  const [evidence, setEvidence] = useState('');
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    let cancelled = false;
    setArtifacts([]); setError('');
    if (run?.run_id) void fetchWorkspaceArtifacts(run.run_id).then(data => { if (!cancelled) setArtifacts(data.artifacts); }).catch(e => { if (!cancelled) setError(String(e)); });
    return () => { cancelled = true; };
  }, [run?.run_id, run?.status, revision]);
  useEffect(() => {
    let cancelled = false;
    setMemories([]);
    if (employeeId) void fetchWorkspaceMemories(employeeId).then(data => { if (!cancelled) setMemories(data.memories.filter(m => m.scope === 'project')); }).catch(e => { if (!cancelled) setError(String(e)); });
    return () => { cancelled = true; };
  }, [employeeId]);
  return <div className="team-run-details">
    {error && <p className="workspace-error" role="alert">{error}</p>}
    {run?.budget && <details><summary>协作用量</summary><p>模型调用 {run.budget.calls}/{run.budget.max_calls} 次 · 计入预算 Token {run.budget.tokens.toLocaleString()}/{run.budget.max_tokens.toLocaleString()}</p></details>}
    <details><summary>团队成果 · {artifacts.length}</summary>{artifacts.length ? artifacts.map(artifact => <details key={artifact.id} className="team-artifact"><summary>{artifact.name} · v{artifact.version}</summary>{artifact.carrier === 'file' ? <a href={workspaceArtifactUrl(artifact.id)} download>下载成果</a> : <MarkdownRenderer content={artifact.body} />}</details>) : <p>交付的成果会保存在这里。</p>}</details>
    {employeeId && <details><summary>项目共享事实 · {memories.length}</summary>{memories.map(memory => <div key={memory.key} className="team-project-fact"><strong>{memory.key}</strong><p>{memory.value}</p><small>{memory.evidence || '用户发布'} · {new Date(memory.updated_at).toLocaleDateString()}</small><div><button type="button" className="btn btn--sm btn--ghost" onClick={() => { setKey(memory.key); setValue(memory.value); setEvidence(memory.evidence); }}>编辑</button><button type="button" className="btn btn--sm btn--ghost" disabled={saving} onClick={async () => { setSaving(true); setError(''); try { await deleteWorkspaceMemory(employeeId, memory.key, 'project'); setMemories(rows => rows.filter(row => row.key !== memory.key)); } catch (failure) { setError(String(failure)); } finally { setSaving(false); } }}>移除</button></div></div>)}
      <form onSubmit={async e => { e.preventDefault(); setSaving(true); setError(''); try { await saveWorkspaceMemory(employeeId, {scope: 'project', key, value, evidence}); const data = await fetchWorkspaceMemories(employeeId); setMemories(data.memories.filter(m => m.scope === 'project')); setKey(''); setValue(''); setEvidence(''); } catch (failure) { setError(String(failure)); } finally { setSaving(false); } }}>
        <input aria-label="共享事实名称" placeholder="名称（同名会更新）" value={key} onChange={e => setKey(e.target.value)} maxLength={100} required />
        <textarea aria-label="项目共享事实" placeholder="发布给项目中所有同事的事实或约定" value={value} onChange={e => setValue(e.target.value)} maxLength={2000} required />
        <input aria-label="事实来源" placeholder="依据或来源" value={evidence} onChange={e => setEvidence(e.target.value)} maxLength={2000} />
        <button type="submit" className="btn btn--sm" disabled={saving || !key.trim() || !value.trim()}>发布共享事实</button>
      </form>
    </details>}
  </div>;
}
