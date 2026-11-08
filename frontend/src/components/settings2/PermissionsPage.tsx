import { useCallback, useEffect, useState } from 'react';
import { Check, RefreshCw, ShieldAlert, ShieldCheck, Trash2 } from '@/components/icons';
import {
  createPermissionRule, deletePermissionRule, fetchApprovalGrants, fetchApprovalRecords,
  fetchPermissionMode, fetchPermissionRules, fetchTools, pauseAIOperations,
  revokeApprovalGrant, savePermissionMode, setPermissionRuleEnabled,
  type ApprovalGrantInfo, type ApprovalRecordInfo,
} from '@/lib/api';
import { useAgentStore } from '@/stores/agentStore';
import { confirmAction } from '@/stores/confirmStore';
import type { PermissionModeResponse, PermissionModeValue, PermissionRule, RuleBehavior, ToolInfo } from '@/types';
import { PageHead, Row, Rows, Section } from './kit';

const message = (error: unknown) => error instanceof Error ? error.message : String(error);
const when = (value: string) => new Date(value).toLocaleString('zh-CN', { hour12: false });

export function PermissionsPage() {
  const [mode, setMode] = useState<PermissionModeResponse | null>(null);
  const [rules, setRules] = useState<PermissionRule[]>([]);
  const [grants, setGrants] = useState<ApprovalGrantInfo[]>([]);
  const [records, setRecords] = useState<ApprovalRecordInfo[]>([]);
  const [tools, setTools] = useState<ToolInfo[]>([]);
  const [auditEnabled, setAuditEnabled] = useState(true);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [behavior, setBehavior] = useState<RuleBehavior>('ask');
  const [tool, setTool] = useState('');
  const [arg, setArg] = useState('');
  const [pattern, setPattern] = useState('');
  const [note, setNote] = useState('');

  const load = useCallback(async () => {
    const [nextMode, nextRules, nextGrants, nextRecords, nextTools] = await Promise.all([
      fetchPermissionMode(), fetchPermissionRules(), fetchApprovalGrants(), fetchApprovalRecords(), fetchTools(),
    ]);
    setMode(nextMode); setRules(nextRules.rules); setGrants(nextGrants.grants);
    setRecords(nextRecords.records); setAuditEnabled(nextRecords.stats.enabled); setTools(nextTools.tools);
  }, []);

  useEffect(() => {
    let alive = true;
    load().catch(e => { if (alive) setError(message(e)); }).finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [load]);

  const act = async (action: () => Promise<unknown>, success: string) => {
    if (busy) return;
    setBusy(true); setError(''); setNotice('');
    try { if (await action() === false) return; setNotice(success); await load(); }
    catch (e) { setError(message(e)); }
    finally { setBusy(false); }
  };

  const choose = async (value: PermissionModeValue) => {
    if (value === mode?.default_mode || busy) return;
    if (value === 'full' && !await confirmAction({
      title: '启用完全访问？', tone: 'warning', confirmLabel: '启用完全访问',
      description: 'AI 将自动执行高风险操作，文件工具可访问当前用户能够访问的目录。明确的询问与禁止规则仍生效。请仅在信任当前任务时开启，使用后可切回受限模式。',
    })) return;
    await act(async () => {
      setMode(await savePermissionMode(value, value === 'full'));
      await useAgentStore.getState().loadPermissionMode();
    }, '权限已保存，后续操作立即按新设置检查；旧临时授权已清除。');
  };

  const titleFor = (name: string) => tools.find(item => item.name === name)?.display_name || name;

  return <div className="tset-page permissions-page">
    <PageHead title="AI 权限与审批" />
    <p className="permissions-intro">决定 AI 可以自行执行哪些操作。需要确认时，你会先看到具体操作、参数和授权范围。</p>
    <div className="permissions-toolbar">
      <span className="permissions-current"><ShieldCheck size={14} />当前：{mode?.label || '读取中…'}</span>
      <button type="button" className="btn btn--sm btn--ghost" disabled={loading || busy} onClick={() => void act(load, '已刷新权限与审批记录。')}><RefreshCw size={12} />刷新</button>
    </div>
    {error && <div className="permissions-message permissions-message--error" role="alert">{error}<button type="button" className="btn btn--sm" disabled={busy} onClick={() => void act(load, '已重新加载。')}>重试</button></div>}
    {notice && <p className="permissions-message" role="status">{notice}</p>}
    {loading ? <p role="status">正在读取权限设置…</p> : <>
      <Section title="默认权限">
        <div className="permissions-modes" role="group" aria-label="默认权限模式">
          {mode?.catalog.map(item => <button key={item.value} type="button" aria-pressed={mode.default_mode === item.value} className={`permissions-mode${mode.default_mode === item.value ? ' is-selected' : ''}${item.value === 'full' ? ' is-full' : ''}`} disabled={busy} onClick={() => void choose(item.value)}>
            <span><strong>{item.label}</strong>{item.value === 'auto' && <em>适合日常任务</em>}{mode.default_mode === item.value && <Check size={15} />}</span>
            <p>{item.hint}</p>
          </button>)}
        </div>
        <p className="permissions-note">默认权限适用于助理、主对话和数字同事。本轮权限可在输入框中临时收紧；电脑操控开关只开放能力，仍遵守这里的权限与规则。</p>
      </Section>

      <Section title="操作范围">
        <Rows>
          <Row label="文件工具" desc={mode?.file_boundary.exempt ? '完全访问已解除文件工具的目录限制。' : '允许访问对话选定的工作目录、以下默认工作目录及已授权目录。'} />
          <Row label="默认工作目录" desc={<code>{mode?.file_boundary.sandbox || '未读取到目录'}</code>} />
          {mode?.file_boundary.allowed_dirs.map(dir => <Row key={dir} label="已授权目录" desc={<code>{dir}</code>} />)}
          <Row label="终端、联网与连接器" desc="终端命令和第三方连接器按工具权限审批；连接器可用范围在插件市场管理。这里的目录限制由文件工具校验，不是操作系统级的进程隔离。" />
        </Rows>
      </Section>

      <Section title="工具规则">
        <p className="permissions-note">禁止优先于其他设置；每次询问优先于自动执行和临时授权。允许规则不能越过只读模式和文件目录边界。</p>
        <form className="permissions-rule-form" onSubmit={event => {
          event.preventDefault();
          if (!tool.trim()) return;
          void act(async () => {
            if (behavior === 'allow' && !await confirmAction({ title: '保存自动允许规则？', tone: 'warning', confirmLabel: '保存规则', description: `规则将允许 ${tool.trim()} 匹配的操作自动执行，并持续生效，直到你停用或删除。${pattern ? `参数 ${arg || '主参数'} 匹配：${pattern}` : '该规则没有参数限制。'}` })) return false;
            await createPermissionRule({ behavior, tool: tool.trim(), arg: arg.trim(), pattern: pattern.trim(), note: note.trim() });
            setTool(''); setArg(''); setPattern(''); setNote('');
          }, '规则已保存。');
        }}>
          <label>处理方式<select value={behavior} onChange={e => setBehavior(e.target.value as RuleBehavior)} disabled={busy}><option value="ask">每次询问</option><option value="deny">始终禁止</option><option value="allow">自动允许</option></select></label>
          <label>工具<input list="permissions-tools" value={tool} onChange={e => setTool(e.target.value)} placeholder="选择工具或输入名称，例如 win_*" required maxLength={80} disabled={busy} /></label>
          <datalist id="permissions-tools">{tools.map(item => <option key={item.name} value={item.name}>{item.display_name || item.name}</option>)}</datalist>
          <label>参数名（可选）<input value={arg} onChange={e => setArg(e.target.value)} placeholder="如 path、command" disabled={busy} /></label>
          <label>参数匹配（可选）<input value={pattern} onChange={e => setPattern(e.target.value)} placeholder="留空匹配全部参数；* 匹配任意内容" maxLength={2000} disabled={busy} /></label>
          <label className="permissions-rule-note">备注（可选）<input value={note} onChange={e => setNote(e.target.value)} placeholder="说明为什么需要这条规则" disabled={busy} /></label>
          <button type="submit" className="btn btn--sm" disabled={busy || !tool.trim()}>添加规则</button>
        </form>
        <p className="permissions-note">匹配支持 * 和 ?。不带通配符时按完整值匹配，目录包含子项请显式写 /*。</p>
        {rules.length === 0 ? <p className="permissions-empty">尚无例外规则，当前按默认权限执行。</p> : <div className="permissions-list">{rules.map(rule => <div key={rule.rule_id} className="permissions-item">
          <div><strong>{rule.behavior_label} · {rule.tool}</strong><p>{rule.pattern ? `${rule.arg || '主参数'} 匹配 ${rule.pattern}` : '所有参数'}{rule.note ? ` · ${rule.note}` : ''}</p></div>
          <button type="button" className="btn btn--sm btn--ghost" disabled={busy} aria-label={`${rule.enabled ? '停用' : '启用'}规则 ${rule.tool}`} onClick={() => void act(() => setPermissionRuleEnabled(rule.rule_id, !rule.enabled), rule.enabled ? '规则已停用。' : '规则已启用。')}>{rule.enabled ? '已启用' : '已停用'}</button>
          <button type="button" className="btn btn--sm btn--ghost" disabled={busy} aria-label={`删除规则 ${rule.tool}`} onClick={() => void act(async () => { if (await confirmAction({ title: '删除这条权限规则？', description: rule.description, confirmLabel: '删除', tone: 'warning' })) await deletePermissionRule(rule.rule_id); }, '规则列表已更新。')}><Trash2 size={13} /></button>
        </div>)}</div>}
      </Section>

      <Section title="临时授权">
        <div className="permissions-toolbar"><p className="permissions-note">在审批中创建，仅限当前会话与请求者；相同工具和全部参数才可复用。到期、次数耗尽或重启后失效。</p><button type="button" className="btn btn--sm btn--ghost" disabled={busy || !grants.length} onClick={() => void act(() => revokeApprovalGrant(), '全部临时授权已撤销。')}>全部撤销</button></div>
        {!grants.length ? <p className="permissions-empty">没有有效的临时授权。</p> : <div className="permissions-list">{grants.map(grant => <div key={grant.grant_id} className="permissions-item"><div><strong>{grant.tool_names.map(titleFor).join('、')}</strong><p>会话 {grant.thread_id}{grant.employee_id ? ` · 请求者 ${grant.employee_id}` : ''}</p><p>还可放行 {grant.remaining} 次 · {when(grant.expires_at)} 到期</p></div><button type="button" className="btn btn--sm btn--ghost" disabled={busy} onClick={() => void act(() => revokeApprovalGrant(grant.grant_id), '临时授权已撤销。')}>撤销</button></div>)}</div>}
      </Section>

      <Section title="最近审批记录">
        {!auditEnabled && <p className="permissions-message permissions-message--error">当前运行配置关闭了审批留痕，新审批不会保存。请启用后端 APPROVAL_RECORD_ENABLED 以记录后续决策。</p>}
        {!records.length ? <p className="permissions-empty">暂无审批记录。需要确认的操作和自动放行的改动会显示在这里。</p> : <div className="permissions-records">{records.map(record => <details key={record.record_id}><summary><span>{titleFor(record.tool_name)}</span><span>{record.decision_label}</span><time>{when(record.requested_at)}</time></summary><div><p>决策来源：{record.decided_by_label}{record.executed ? ` · ${record.success ? '执行成功' : '执行失败'}` : ''}</p><p>会话：{record.thread_id}{record.employee_id ? ` · 请求者：${record.employee_id}` : ''}</p>{record.reason && <p>{record.reason}</p>}<pre>{JSON.stringify(record.args, null, 2)}</pre></div></details>)}</div>}
        <p className="permissions-note">显示最近 50 条；点刷新更新记录。已批准表示获得执行许可，执行结果以工具返回为准。</p>
      </Section>

      <Section title="暂停 AI 操作">
        <div className="permissions-pause"><ShieldAlert size={22} /><div><strong>立即切换为只读</strong><p>撤销全部临时授权并拒绝等待中的审批，后续改动将被拦截。已经开始的命令或已经完成的操作无法由此撤回。</p></div><button type="button" className="btn btn--sm btn--danger" disabled={busy} onClick={() => void act(async () => { setMode(await pauseAIOperations()); await useAgentStore.getState().loadPermissionMode(); }, '已暂停 AI 改动，切换为只读模式。')}>暂停操作</button></div>
      </Section>
    </>}
  </div>;
}

export default PermissionsPage;
