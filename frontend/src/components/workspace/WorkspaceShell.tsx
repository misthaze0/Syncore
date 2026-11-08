import { confirmAction } from '@/stores/confirmStore';
import { useEffect, useRef, useState, type ReactNode } from 'react';
import { NavLink, useLocation, useNavigate } from 'react-router-dom';
import { ChevronDown, Layers, MessageSquare, Pencil, Plus, Trash, Users, X } from '@/components/icons';
import { EmployeeAvatar } from './EmployeeAvatar';
import { EmptyState } from '@/components/EmptyState';
import { PageHeader } from '@/components/PageShell';
import { EmployeeCapabilities } from './EmployeeCapabilities';
import { useModalFocus } from '@/hooks/useModalFocus';
import { fetchModels, shortModelName } from '@/lib/api';
import { selectedEmployeeCapabilities, suggestEmployeeModel } from '@/lib/workspace';
import { useWorkspaceStore } from '@/stores/workspaceStore';
import type { BaseAgent, Employee, ModelOption } from '@/types';

export function WorkspaceShell({ workspaceId, children }: { workspaceId?: string; children?: ReactNode | ((addEmployee: () => void) => ReactNode) }) {
  const navigate = useNavigate();
  const location = useLocation();
  const store = useWorkspaceStore();
  const [createOpen, setCreateOpen] = useState(false);
  const [editor, setEditor] = useState<Employee | 'new' | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [navOpen, setNavOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  const selected = store.workspaces.find(ws => ws.id === (workspaceId || store.selectedWorkspaceId));
  useEffect(() => { if (!useWorkspaceStore.getState().workspaces.length) void store.loadWorkspaces(); }, [store.loadWorkspaces]);
  useEffect(() => { if (workspaceId) store.selectWorkspace(workspaceId); }, [workspaceId, store.selectWorkspace]);
  useEffect(() => {
    if (!menuOpen) return;
    const close = (event: PointerEvent) => { if (!menuRef.current?.contains(event.target as Node)) setMenuOpen(false); };
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') setMenuOpen(false); };
    document.addEventListener('pointerdown', close); document.addEventListener('keydown', escape);
    return () => { document.removeEventListener('pointerdown', close); document.removeEventListener('keydown', escape); };
  }, [menuOpen]);
  useEffect(() => { setNavOpen(false); }, [location.pathname]);
  const employees = store.employees.filter(e => e.workspace_id === selected?.id);
  const groups = store.groups.filter(g => g.workspace_id === selected?.id);
  return <div className={`main workspace-hub${navOpen ? ' workspace-hub--nav-open' : ''}`}>
    <div className="workspace-mobile-bar"><button type="button" onClick={() => setNavOpen(value => !value)} aria-expanded={navOpen}><Layers size={16} />{selected?.name || '工作空间'}<ChevronDown size={13} /></button></div>
    <aside className="workspace-nav" aria-label="工作空间会话">
      <div className="workspace-nav__switcher" ref={menuRef}>
        <button type="button" className="workspace-switch" onClick={() => setMenuOpen(value => !value)} aria-expanded={menuOpen}>
          <span className="workspace-switch__mark"><Layers size={19} /></span><span><small>工作空间</small><strong>{selected?.name || '我的团队'}</strong></span><ChevronDown size={14} />
        </button>
        {menuOpen && <div className="workspace-switch__menu">
          {store.workspaces.map(ws => <button type="button" key={ws.id} onClick={() => { store.selectWorkspace(ws.id); setMenuOpen(false); navigate('/workspace'); }}><span>{ws.name}</span>{ws.id === selected?.id && <span>✓</span>}</button>)}
          <button type="button" onClick={() => { setMenuOpen(false); setCreateOpen(true); }}><Plus size={14} />新建工作空间</button>
          {selected && <button type="button" className="workspace-switch__delete" onClick={async () => {
            if (await confirmAction({ title: "删除这个工作空间？", description: `「${selected.name}」及其所有群聊和员工将被永久删除，此操作无法撤销。` })) void store.deleteWorkspace(selected.id).then(() => { if (useWorkspaceStore.getState().error) return; setMenuOpen(false); navigate('/workspace'); void store.loadWorkspaces(); });
          }}><Trash size={13} />删除此工作空间</button>}
        </div>}
      </div>
      <div className="workspace-nav__section"><span>工作群</span><span>{groups.length}</span></div>
      <nav className="workspace-nav__items" aria-label="工作群">
        {groups.map(group => <NavLink key={group.id} to={`/workspace/groups/${group.id}`} className={({ isActive }) => `workspace-channel${isActive ? ' is-active' : ''}`}>
          <span className="workspace-channel__icon"><MessageSquare size={17} /></span><span className="workspace-channel__body"><strong>{group.name}</strong><small>{group.member_count || 0} 位成员</small></span>
        </NavLink>)}
      </nav>
      <div className="workspace-nav__section"><span>数字同事 <em>{employees.length}</em></span><button type="button" title="添加数字员工" aria-label="添加数字员工" disabled={!selected} onClick={() => setEditor('new')}><Plus size={15} /></button></div>
      <nav className="workspace-nav__items" aria-label="员工私聊">
        {employees.map(employee => <div className="workspace-person" key={employee.id}>
          <NavLink to={`/workspace/employees/${employee.id}`} className={({ isActive }) => `workspace-channel${isActive ? ' is-active' : ''}`}>
            <EmployeeAvatar name={employee.name} /><span className="workspace-channel__body"><strong>{employee.name}{!employee.enabled && <small> · 已停用</small>}</strong><small>{employee.tagline || shortModelName(employee.model || '默认模型')}</small></span>
          </NavLink><button type="button" className="workspace-person__edit" aria-label={`编辑${employee.name}`} title="编辑员工" onClick={() => setEditor(employee)}><Pencil size={12} /></button>
        </div>)}
        {selected && <button type="button" className="workspace-invite" onClick={() => setEditor('new')}><Plus size={16} />添加同事</button>}
      </nav>
      <div className="workspace-nav__footer"><span className="workspace-presence" />{employees.filter(e => e.enabled).length} 位同事可用</div>
    </aside>
    <div className="workspace-content">{(typeof children === 'function' ? children(() => setEditor('new')) : children) || <div className="main workspace-home">
      <PageHeader title="工作空间" icon={Users} />
      <div className="workspace-home__body">
        {store.error && <div className="workspace-error" role="alert">{store.error}</div>}
        <EmptyState fill icon={Users} title={store.workspacesLoading ? '正在打开工作空间…' : '暂无工作空间'} action={!store.workspacesLoading && <button type="button" className="btn btn--primary" onClick={() => setCreateOpen(true)}><Plus size={16} />新建工作空间</button>} />
      </div>
    </div>}</div>
    {createOpen && <CreateWorkspaceDialog onClose={() => setCreateOpen(false)} />}
    {editor && selected && <EmployeeEditor workspaceId={selected.id} employee={editor === 'new' ? null : editor} groupId={groups[0]?.id} onClose={() => setEditor(null)} />}
  </div>;
}

function WorkspaceDialog({ title, children, onClose }: { title: string; children: ReactNode; onClose: () => void }) {
  const ref = useRef<HTMLDivElement>(null);
  useModalFocus(ref);
  return <div className="ws-modal" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }} onKeyDown={event => { if (event.key === 'Escape') onClose(); }}>
    <div className="ws-modal__card team-dialog" ref={ref} role="dialog" aria-modal="true" aria-label={title} tabIndex={-1}><div className="ws-modal__head"><span>{title}</span><button type="button" onClick={onClose} aria-label="关闭"><X size={18} /></button></div>{children}</div>
  </div>;
}

function CreateWorkspaceDialog({ onClose }: { onClose: () => void }) {
  const [name, setName] = useState(''); const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  const navigate = useNavigate();
  return <WorkspaceDialog title="新建工作空间" onClose={onClose}><form onSubmit={async event => {
    event.preventDefault(); if (!name.trim() || busy) return; setBusy(true);
    const ws = await useWorkspaceStore.getState().createWorkspace(name.trim(), ''); setBusy(false);
    if (ws) { onClose(); navigate('/workspace'); } else setError(useWorkspaceStore.getState().error || '创建失败，请重试');
  }}>
    <label className="ws-field"><span>团队名称</span><input autoFocus maxLength={60} placeholder="例如：产品工作室" value={name} onChange={event => setName(event.target.value)} required /></label>
    {error && <p className="workspace-error" role="alert">{error}</p>}
    <div className="ws-modal__foot"><button type="button" className="btn btn--ghost" onClick={onClose}>取消</button><button type="submit" className="btn btn--primary" disabled={busy || !name.trim()}>{busy ? '创建中…' : '创建工作空间'}</button></div>
  </form></WorkspaceDialog>;
}

const ROLES: Array<{ name: string; tagline: string; base: BaseAgent }> = [
  { name: '研究员', tagline: '查找资料，核实事实，给出有依据的结论', base: 'browser_agent' },
  { name: '策划', tagline: '梳理需求，把想法变成可执行的方案', base: 'file_agent' },
  { name: '工程师', tagline: '编写代码，实现方案并验证结果', base: 'pc_agent' },
];

function EmployeeEditor({ workspaceId, employee, groupId, onClose }: { workspaceId: string; employee: Employee | null; groupId?: string; onClose: () => void }) {
  const [name, setName] = useState(employee?.name || ''); const [tagline, setTagline] = useState(employee?.tagline || '');
  const [model, setModel] = useState(employee?.model || ''); const [profile, setProfile] = useState(employee?.model_profile_id || '');
  const [models, setModels] = useState<ModelOption[]>([]); const [modelError, setModelError] = useState('');
  const [base, setBase] = useState<BaseAgent>(employee?.base_agent || 'file_agent'); const [tone, setTone] = useState(employee?.tone || 'professional');
  const [toneCustom, setToneCustom] = useState(employee?.tone_custom || ''); const [instructions, setInstructions] = useState(employee?.instructions || '');
  const [scope, setScope] = useState(employee?.scope || ''); const [allowed, setAllowed] = useState(employee?.tools_allowed || []);
  const [enabled, setEnabled] = useState(employee?.enabled ?? true); const [join, setJoin] = useState(true);
  const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  const [createdId, setCreatedId] = useState('');
  const modelTouched = useRef(false);
  const store = useWorkspaceStore(); const navigate = useNavigate(); const location = useLocation();
  useEffect(() => {
    void fetchModels().then(data => {
      setModels(data.models);
      if (!employee && !modelTouched.current) {
        const team = useWorkspaceStore.getState().employees.filter(item => item.workspace_id === workspaceId);
        const suggested = suggestEmployeeModel(data.models, team, data.default, data.active_profile_id || '');
        if (suggested) { setModel(suggested.id); setProfile(suggested.profile_id || ''); }
      }
    }).catch(() => setModelError('模型列表暂不可用'));
    void store.loadTools();
  }, [employee, workspaceId, store.loadTools]);
  const key = JSON.stringify([profile, model]);
  const selectedCapabilities = selectedEmployeeCapabilities(store.tools, allowed);
  const options = models.some(item => item.id === model && (item.profile_id || '') === profile) || !model ? models : [{ id: model, label: model, profile_id: profile, default: false }, ...models];
  return <WorkspaceDialog title={employee ? '编辑同事' : '添加数字同事'} onClose={onClose}><form onSubmit={async event => {
    event.preventDefault(); if (!name.trim() || busy || store.toolsLoading || store.toolsError) return; setBusy(true); setError('');
    const payload = { name: name.trim(), tagline: tagline.trim(), model, model_profile_id: model ? profile : '', base_agent: base, tone, tone_custom: toneCustom, instructions, scope, tools_allowed: selectedCapabilities, enabled };
    const state = useWorkspaceStore.getState(); let ok = false; let id = employee?.id || createdId;
    if (employee || createdId) ok = await state.updateEmployee(employee?.id || createdId, payload);
    else { const created = await state.createEmployee({ workspace_id: workspaceId, ...payload }); ok = !!created; if (created) { id = created.id; setCreatedId(created.id); } }
    if (ok && !employee && join && groupId && id) {
      await state.addMember(groupId, id);
      if (useWorkspaceStore.getState().error) { setError('同事已创建，加入群聊失败，请重试。'); setBusy(false); return; }
    }
    setBusy(false);
    if (ok) { void state.loadWorkspaceDetail(); if (groupId) void state.openGroup(groupId); onClose(); }
    else { setError(useWorkspaceStore.getState().error || '保存失败，请重试'); void state.loadTools(); }
  }}>
    {!employee && <div className="team-role-presets">{ROLES.map(role => <button type="button" key={role.name} className={name === role.name ? 'is-active' : ''} onClick={() => { setName(role.name); setTagline(role.tagline); setBase(role.base); }}>{role.name}<Plus size={12} /></button>)}</div>}
    <div className="team-editor__identity"><EmployeeAvatar name={name || '同事'} size="large" /><label className="ws-field"><span>姓名</span><input value={name} onChange={event => setName(event.target.value)} placeholder="给同事起个名字" maxLength={24} required autoFocus /></label></div>
    <label className="ws-field"><span>负责什么</span><input value={tagline} onChange={event => setTagline(event.target.value)} placeholder="例如：调研市场，核实信息" maxLength={60} /></label>
    <label className="ws-field"><span>对话模型</span><select value={key} onChange={event => { modelTouched.current = true; const [p, m] = JSON.parse(event.target.value) as string[]; setProfile(p); setModel(m); }}>
      <option value={JSON.stringify(['', ''])}>跟随默认模型</option>{options.map(option => <option key={JSON.stringify([option.profile_id || '', option.id])} value={JSON.stringify([option.profile_id || '', option.id])}>{option.provider ? `${option.provider} · ` : ''}{shortModelName(option.label || option.id)}</option>)}
    </select></label>
    {modelError && <small className="workspace-error">{modelError}</small>}<button type="button" className="team-model-link" onClick={() => { onClose(); navigate('/settings/models', { state: { background: location } }); }}>管理模型连接 ↗</button>
    <EmployeeCapabilities options={store.tools} selected={selectedCapabilities} loading={store.toolsLoading} error={store.toolsError} onToggle={key => setAllowed(selectedCapabilities.includes(key) ? selectedCapabilities.filter(item => item !== key) : [...selectedCapabilities, key])} onRetry={() => void store.loadTools()} onMarket={() => { onClose(); navigate('/market?view=installed'); }} />
    <details className="team-advanced"><summary>更多设置</summary><div className="ws-form-grid">
      <label className="ws-field"><span>工作能力</span><select value={base} onChange={event => setBase(event.target.value as BaseAgent)}><option value="file_agent">文档与资料</option><option value="browser_agent">浏览与调研</option><option value="pc_agent">编程与执行</option><option value="software_agent">软件操作</option></select></label>
      <label className="ws-field"><span>交流风格</span><select value={tone} onChange={event => setTone(event.target.value)}><option value="professional">专业</option><option value="friendly">轻松</option><option value="concise">简洁</option><option value="humorous">幽默</option><option value="custom">自定义</option></select></label>
      {tone === 'custom' && <label className="ws-field ws-field--full"><span>自定义风格</span><input value={toneCustom} maxLength={200} onChange={event => setToneCustom(event.target.value)} /></label>}
      <label className="ws-field ws-field--full"><span>工作要求</span><textarea rows={2} maxLength={1000} value={instructions} onChange={event => setInstructions(event.target.value)} /></label>
      <label className="ws-field ws-field--full"><span>职责边界</span><textarea rows={2} maxLength={1000} value={scope} onChange={event => setScope(event.target.value)} /></label>
    </div>
      <label className="team-check"><input type="checkbox" checked={enabled} onChange={event => setEnabled(event.target.checked)} />参与团队工作</label>
    </details>
    {!employee && groupId && <label className="team-check"><input type="checkbox" checked={join} onChange={event => setJoin(event.target.checked)} />加入工作群</label>}
    {error && <p className="workspace-error" role="alert">{error}</p>}
    <div className="ws-modal__foot">{employee && <button type="button" className="btn btn--ghost team-delete" onClick={async () => { if (await confirmAction({ title: "删除这位同事？", description: `「${employee.name}」将从工作空间中移除，此操作无法撤销。` })) void store.deleteEmployee(employee.id).then(() => { if (!useWorkspaceStore.getState().error) { void store.loadWorkspaceDetail(); onClose(); navigate('/workspace'); } else setError(useWorkspaceStore.getState().error || '删除失败'); }); }}>删除同事</button>}<button type="button" className="btn btn--ghost" onClick={onClose}>取消</button><button type="submit" className="btn btn--primary" disabled={busy || !name.trim() || store.toolsLoading || !!store.toolsError}>{busy ? '保存中…' : employee ? '保存' : '添加同事'}</button></div>
  </form></WorkspaceDialog>;
}
