import { confirmAction } from '@/stores/confirmStore';
/**
 * 专员编辑器 —— 一个智能体「是谁、能碰什么、怎么跑」的全部可定制面。
 *
 * 设计取舍（对标 Claude Code 的 subagent 定义与 OpenAI Agents SDK 的 Agent）：
 * - 一个专员 = 身份（显示名/领域/职责）+ 路由（能力名）+ 权限（工具）+ 运行时（模型/温度/步数/检索）。
 *   业界两份参考在这四个维度上完全一致，所以照它们分区分组，用户从别处来都能对上。
 * - **调度视角与执行视角分开填**（`planner_hint` vs `responsibility`）。
 *   合成一句话的后果是调度 prompt 里塞满执行细节 —— 后端把两个字段分开存，这里也分开给。
 * - 工具默认「跟随工具表」（各工具自报 allowed_agents）。「显式指定」是**整份替换**这份依据：
 *   后端 `_visible()` 见到白名单就以白名单为准，所以它既能去掉默认拿到的，也能给到工具表
 *   没自报给这个专员的 —— 新建的专员默认零工具，只有这条路能给它工具。
 *   inherit 模式因此只读展示生效清单、不给勾选：勾了却因模式而不生效，比不给点更让人困惑。
 * - 长度上限在前端复述了一份（与 `registry.py` 一致）。超限后端是**截断**而不是报错，
 *   所以输入框用 maxLength 挡住，别让「保存了但少了几个字」这种事发生。
 * - 保存后 draft 用**后端返回的那份**重建：截断、能力去重、数字清洗都发生在那一侧，
 *   于是界面显示的永远是实际落盘的配置。
 */

import { useCallback, useEffect, useMemo, useState } from 'react';

import { ArrowLeft, Check, RotateCcw, Trash, X } from '@/components/icons';
import {
  createAgent,
  deleteAgent,
  fetchAgentPrompt,
  resetAgent,
  updateAgent,
} from '@/lib/api';
import { capabilityTitle, DANGER_LABELS } from '@/lib/extensions';
import type {
  AgentDef,
  AgentPayload,
  AgentPromptResponse,
  ModelOption,
  ToolInfo,
} from '@/types';
import { Row, Rows, Section, Switch, Table, type TableColumn } from './kit';

/** 与后端 `registry.py` 的同名常量一致（超限后端会静默截断，所以这里先挡住） */
const LIMIT = {
  name: 32,
  display: 24,
  domain: 20,
  responsibility: 200,
  plannerHint: 120,
  capability: 12,
  capabilities: 8,
};

const NAME_RE = /^[a-z][a-z0-9_]{1,31}$/;

interface Draft {
  name: string;
  display_name: string;
  domain: string;
  responsibility: string;
  planner_hint: string;
  capabilities: string[];
  tool_mode: 'inherit' | 'explicit';
  tools_allowed: string[];
  model: string;
  /** 空串 = 跟随现状（后端 runner 的 temperature=0） */
  temperature: string;
  /** 空串 = 跟随全局 */
  max_steps: string;
  retrieval: boolean;
  enabled: boolean;
}

const BLANK: Draft = {
  name: '',
  display_name: '',
  domain: '',
  responsibility: '',
  planner_hint: '',
  capabilities: [],
  tool_mode: 'inherit',
  tools_allowed: [],
  model: '',
  temperature: '',
  max_steps: '',
  retrieval: false,
  enabled: true,
};

function toDraft(a: AgentDef): Draft {
  return {
    name: a.name,
    display_name: a.display_name,
    domain: a.domain,
    responsibility: a.responsibility,
    planner_hint: a.planner_hint,
    capabilities: [...a.capabilities],
    tool_mode: a.tool_mode,
    tools_allowed: [...a.tools_allowed],
    model: a.model,
    temperature: a.temperature === null || a.temperature === undefined ? '' : String(a.temperature),
    max_steps: a.max_steps === null || a.max_steps === undefined ? '' : String(a.max_steps),
    retrieval: a.retrieval,
    enabled: a.enabled,
  };
}

export function AgentEditor({
  agent,
  tools,
  models,
  globalModel,
  globalMaxSteps,
  onSaved,
  onDeleted,
  onBack,
}: {
  /** null = 新建 */
  agent: AgentDef | null;
  tools: ToolInfo[];
  models: ModelOption[];
  globalModel: string;
  globalMaxSteps: number;
  onSaved: (saved: AgentDef) => void;
  onDeleted: (name: string) => void;
  onBack: () => void;
}) {
  // 初值来自 props，之后**不再跟随 props 变化**：切换编辑对象由父组件的 `key` 重挂载完成。
  // 用 useEffect 同步会把保存后刚显示的「已创建 / 已保存」提示一起抹掉 ——
  // 而那条提示正是用户最需要的确认。
  const [draft, setDraft] = useState<Draft>(() => (agent ? toDraft(agent) : BLANK));
  const [base, setBase] = useState<AgentDef | null>(agent);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [prompt, setPrompt] = useState<AgentPromptResponse | null>(null);
  const [capInput, setCapInput] = useState('');

  const loadPrompt = useCallback(async (name: string) => {
    try {
      setPrompt(await fetchAgentPrompt(name));
    } catch (e) {
      // 预览读不到不该让编辑界面报错：身份段是「确认后要看原文」的出口，不是编辑前提
      setPrompt(null);
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  // 打开时读一次；保存/恢复默认后 base 换新对象，再读一次（预览必须是**落盘后**的那份）
  useEffect(() => {
    if (base) void loadPrompt(base.name);
    else setPrompt(null);
  }, [base, loadPrompt]);

  const isNew = base === null;

  // 编辑态的标识是主键、不可改，所以它必然是合法的
  const nameOk = isNew ? NAME_RE.test(draft.name.trim()) : true;
  const tempNum = draft.temperature.trim() === '' ? null : Number(draft.temperature);
  const tempOk = tempNum === null || (Number.isFinite(tempNum) && tempNum >= 0 && tempNum <= 2);
  const stepsNum = draft.max_steps.trim() === '' ? null : Number(draft.max_steps);
  const stepsOk = stepsNum === null || (Number.isInteger(stepsNum) && stepsNum >= 1 && stepsNum <= 50);
  const capsFull = draft.capabilities.length >= LIMIT.capabilities;

  /** 只提交差异：与 `/persona` 同一套部分更新语义，未填的字段保持原值 */
  const buildChanges = useCallback((): AgentPayload => {
    const c: AgentPayload = {};
    const b: AgentDef | null = base;
    if (draft.display_name !== (b?.display_name ?? '')) c.display_name = draft.display_name;
    if (draft.domain !== (b?.domain ?? '')) c.domain = draft.domain;
    if (draft.responsibility !== (b?.responsibility ?? '')) c.responsibility = draft.responsibility;
    if (draft.planner_hint !== (b?.planner_hint ?? '')) c.planner_hint = draft.planner_hint;
    if (!b || b.capabilities.join('\u0000') !== draft.capabilities.join('\u0000'))
      c.capabilities = draft.capabilities;
    if (!b || b.tools_allowed.join('\u0000') !== draft.tools_allowed.join('\u0000') || b.tool_mode !== draft.tool_mode)
      c.tools_allowed = draft.tool_mode === 'explicit' ? draft.tools_allowed : [];
    if (draft.model !== (b?.model ?? '')) c.model = draft.model;
    if (tempNum !== (b?.temperature ?? null)) c.temperature = tempNum;
    if (stepsNum !== (b?.max_steps ?? null)) c.max_steps = stepsNum;
    if (draft.retrieval !== (b?.retrieval ?? false)) c.retrieval = draft.retrieval;
    if (draft.enabled !== (b?.enabled ?? true)) c.enabled = draft.enabled;
    return c;
  }, [base, draft, tempNum, stepsNum]);

  /**
   * 「未保存」直接由**要提交的那份差异**推出来。
   *
   * 不做字段逐项比对（那会把「0.0」vs「0」这种同值不同串判成脏），
   * 也不与打开时的快照比 —— 保存后继续改必须依然显示得如实。
   * 新建时只填了标识也算脏：标识不在 changes 里，但它是创建的必要条件。
   */
  const dirty = useMemo(() => {
    if (Object.keys(buildChanges()).length > 0) return true;
    return isNew && draft.name.trim() !== '';
  }, [buildChanges, isNew, draft.name]);

  const canSave = dirty && !saving && nameOk && tempOk && stepsOk;

  const onSave = useCallback(async () => {
    if (!canSave) return;
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const changes = buildChanges();
      const res = isNew
        ? await createAgent({ ...changes, name: draft.name.trim().toLowerCase() })
        : await updateAgent(base.name, changes);
      if (!res.agent) throw new Error('后端没有返回这个专员');
      // 用后端返回的那份重建草稿：截断/去重发生在那一侧，界面必须显示落盘后的真实值
      setBase(res.agent);
      setDraft(toDraft(res.agent));
      onSaved(res.agent);
      setNotice(
        isNew
          ? `已创建「${res.agent.display_name}」，下一轮对话即生效`
          : '已保存，下一轮对话即生效（正在跑的子任务用的仍是派发那一刻的配置）',
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }, [canSave, buildChanges, isNew, base, draft.name, onSaved]);

  const onReset = useCallback(async () => {
    if (!base) return;
    if (!await confirmAction({
      title: base.builtin ? '恢复默认专员设置？' : '删除这个自定义专员？',
      description: base.builtin ? '你修改过的属性将被替换为内置默认值。' : `「${base.display_name}」将从可用专员中移除，此操作无法撤销。`,
      confirmLabel: base.builtin ? '恢复默认' : '删除',
      tone: base.builtin ? 'warning' : 'danger',
    })) return;
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      if (base.builtin) {
        const res = await resetAgent(base.name);
        if (res.agent) {
          setBase(res.agent);
          setDraft(toDraft(res.agent));
          onSaved(res.agent);
        }
        setNotice('已恢复默认');
      } else {
        await deleteAgent(base.name);
        onDeleted(base.name);
        onBack();
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }, [base, onSaved, onDeleted, onBack]);

  const onDelete = useCallback(async () => {
    if (!base || base.builtin) return;
    if (!await confirmAction({ title: '删除这个专员？', description: `「${base.display_name}」将从可用专员中移除，此操作无法撤销。` })) return;
    setSaving(true);
    setError(null);
    try {
      await deleteAgent(base.name);
      onDeleted(base.name);
      onBack();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }, [base, onDeleted, onBack]);

  const back = useCallback(async () => {
    if (dirty && !await confirmAction({ title: '放弃未保存的修改？', description: '离开后，这次对专员设置的修改将丢失。', confirmLabel: '放弃修改', cancelLabel: '继续编辑', tone: 'warning' })) return;
    onBack();
  }, [dirty, onBack]);

  const addCapability = useCallback(
    (raw: string) => {
      const cap = raw.trim().slice(0, LIMIT.capability);
      if (!cap || /^\d+$/.test(cap)) return;
      setDraft((d) =>
        d.capabilities.includes(cap) || d.capabilities.length >= LIMIT.capabilities
          ? d
          : { ...d, capabilities: [...d.capabilities, cap] },
      );
      setCapInput('');
    },
    [],
  );

  const effectiveRows: ToolInfo[] = useMemo(() => {
    const names = base?.effective_tools ?? [];
    const known = new Map(tools.map((t) => [t.name, t]));
    return names.map(
      (n) =>
        known.get(n) ??
        // 工具表里查不到的名字也要显示出来 —— 它是「白名单里有个已经不存在的工具」
        // 这类问题的唯一线索，藏起来等于把故障现场抹掉。
        ({
          name: n,
          description: '（工具表当前没有这个工具）',
          danger_level: 'safe' as const,
          allowed_agents: [],
          version: '',
          health: 'unknown' as const,
        }),
    );
  }, [base, tools]);

  const toolColumns: TableColumn[] = [
    { key: 'name', title: '工具' },
    { key: 'danger', title: '等级', width: 70 },
    { key: 'desc', title: '说明' },
  ];

  return (
    <div className="tset tset-page">
      <div className="ag-ed__head">
        <button type="button" className="btn btn--sm btn--ghost" onClick={back}>
          <ArrowLeft size={13} /> 返回专员列表
        </button>
        <span className="tset-page__title">
          {isNew ? '新建专员' : base.display_name}
          {!isNew && <code className="ag-ed__id">{base.name}</code>}
        </span>
        {dirty && <span className="badge badge--accent">未保存</span>}
      </div>

      {error && <div className="alert alert--error">{error}</div>}
      {notice && <div className="alert alert--info">{notice}</div>}

      <Section title="身份">
        <Rows>
          <Row
            label="标识"
            title={
              isNew
                ? '小写字母开头，可用数字与下划线，2-32 位；创建后不可改'
                : '创建后不可改'
            }
            control={
              <input
                className="input input--narrow"
                value={draft.name}
                maxLength={LIMIT.name}
                spellCheck={false}
                placeholder="translate_agent"
                disabled={!isNew}
                onChange={(e) => setDraft({ ...draft, name: e.target.value.toLowerCase() })}
              />
            }
          />
          <Row
            label="显示名"
            control={
              <input
                className="input input--narrow"
                value={draft.display_name}
                maxLength={LIMIT.display}
                placeholder="翻译专员"
                onChange={(e) => setDraft({ ...draft, display_name: e.target.value })}
              />
            }
          />
          <Row
            label="领域"
            control={
              <input
                className="input input--narrow"
                value={draft.domain}
                maxLength={LIMIT.domain}
                placeholder="文本翻译"
                onChange={(e) => setDraft({ ...draft, domain: e.target.value })}
              />
            }
          />
          <Row
            stack
            label="职责"
            control={
              <textarea
                className="persona-form__area"
                rows={3}
                maxLength={LIMIT.responsibility}
                title={`最多 ${LIMIT.responsibility} 字`}
                value={draft.responsibility}
                placeholder="例如：把用户给的中文译成地道英文，保留原文语气，不额外解释语法"
                onChange={(e) => setDraft({ ...draft, responsibility: e.target.value })}
              />
            }
          />
          <Row
            stack
            label="调度说明"
            control={
              <textarea
                className="persona-form__area"
                rows={2}
                maxLength={LIMIT.plannerHint}
                title={`最多 ${LIMIT.plannerHint} 字`}
                value={draft.planner_hint}
                placeholder="例如：用户要求把中文材料译成英文时"
                onChange={(e) => setDraft({ ...draft, planner_hint: e.target.value })}
              />
            }
          />
          <Row
            stack
            label="能力名"
            title="调度清单里显示的名称；不要与已有能力名重复"
            desc={capsFull ? `已达 ${LIMIT.capabilities} 个上限` : undefined}
            control={
              <div className="ag-caps">
                {draft.capabilities.map((cap) => (
                  <span key={cap} className="ag-cap">
                    {cap}
                    <button
                      type="button"
                      className="ag-cap__x"
                      title="移除"
                      onClick={() =>
                        setDraft({
                          ...draft,
                          capabilities: draft.capabilities.filter((c) => c !== cap),
                        })
                      }
                    >
                      <X size={11} />
                    </button>
                  </span>
                ))}
                <input
                  className="input ag-caps__input"
                  value={capInput}
                  maxLength={LIMIT.capability}
                  placeholder={capsFull ? '已达上限' : '查资料，回车添加'}
                  disabled={capsFull}
                  onChange={(e) => setCapInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ',') {
                      e.preventDefault();
                      addCapability(capInput);
                    } else if (e.key === 'Backspace' && !capInput && draft.capabilities.length) {
                      setDraft({
                        ...draft,
                        capabilities: draft.capabilities.slice(0, -1),
                      });
                    }
                  }}
                  onBlur={() => capInput.trim() && addCapability(capInput)}
                />
              </div>
            }
          />
        </Rows>
      </Section>

      <Section title="运行时">
        <Rows>
          <Row
            label="模型"
            control={
              <select
                className="select"
                value={draft.model}
                disabled={models.length <= 1}
                onChange={(e) => setDraft({ ...draft, model: e.target.value })}
              >
                <option value="">跟随全局（{globalModel || '未配置'}）</option>
                {models.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.label}
                  </option>
                ))}
              </select>
            }
          />
          <Row
            label="温度"
            control={
              <input
                className="input input--narrow"
                value={draft.temperature}
                inputMode="decimal"
                placeholder="留空 = 0"
                title="0-2，留空为 0"
                onChange={(e) => setDraft({ ...draft, temperature: e.target.value })}
              />
            }
          />
          <Row
            label="步数上限"
            control={
              <input
                className="input input--narrow"
                value={draft.max_steps}
                inputMode="numeric"
                placeholder={`跟随全局 ${globalMaxSteps} 步`}
                title="1-50，留空跟随全局"
                onChange={(e) => setDraft({ ...draft, max_steps: e.target.value })}
              />
            }
          />
          <Row
            label="知识库检索"
            title="规划前先检索知识库，命中内容并入提示词"
            control={
              <Switch
                checked={draft.retrieval}
                ariaLabel="知识库检索"
                onChange={(v) => setDraft({ ...draft, retrieval: v })}
              />
            }
          />
          <Row
            label="启用"
            title={
              base?.resident
                ? '调度入口不可停用'
                : '停用后从调度清单移除，定义保留'
            }
            control={
              <Switch
                checked={draft.enabled}
                disabled={Boolean(base?.resident)}
                ariaLabel="启用"
                onChange={(v) => setDraft({ ...draft, enabled: v })}
              />
            }
          />
        </Rows>
        {/* 校验提示放在卡片外：`.set-rows` 是带边框的一体卡片，行之间插一条 alert 会把它劈成两截 */}
        {!tempOk && <div className="alert alert--warn">温度要填 0 到 2 之间的数字。</div>}
        {!stepsOk && <div className="alert alert--warn">步数要填 1 到 50 的整数。</div>}
        {!nameOk && (
          <div className="alert alert--warn">
            标识只能用小写字母、数字与下划线，需以字母开头，长度 2-32。
          </div>
        )}
      </Section>

      <Section title="工具权限">
        <div className="ag-seg">
          <button
            type="button"
            className={`chip-filter${draft.tool_mode === 'inherit' ? ' chip-filter--active' : ''}`}
            title="按工具声明的归属"
            onClick={() => setDraft({ ...draft, tool_mode: 'inherit' })}
          >
            跟随工具表
          </button>
          <button
            type="button"
            className={`chip-filter${draft.tool_mode === 'explicit' ? ' chip-filter--active' : ''}`}
            title="自定义白名单，整份替换「跟随工具表」；danger 工具仍需审批"
            onClick={() =>
              setDraft({
                ...draft,
                tool_mode: 'explicit',
                // 切过来时把当前生效清单当起点：让用户在已有权限上增删，
                // 而不是从「一个工具都没有」开始勾 —— 那会让这个专员立刻干不了活。
                tools_allowed: draft.tools_allowed.length
                  ? draft.tools_allowed
                  : (base?.effective_tools ?? []),
              })
            }
          >
            显式指定
          </button>
          <span className="ag-seg__hint">
            {isNew && draft.tool_mode === 'inherit'
              ? '尚未落盘，无法计算生效工具'
              : `生效 ${effectiveRows.length} 个`}
          </span>
        </div>

        {isNew && draft.tool_mode === 'inherit' ? (
          <div className="empty-hint">创建后显示工具归属</div>
        ) : draft.tool_mode === 'inherit' ? (
          <Table
            columns={toolColumns}
            rows={effectiveRows}
            rowKey={(row) => row.name}
            render={(row, col) => {
              if (col.key === 'name')
                return (
                  <span className="ag-tool-name">
                    <span>{capabilityTitle(row)}</span>
                    <code className="ag-code">{row.name}</code>
                  </span>
                );
              if (col.key === 'danger') return <DangerTag level={row.danger_level} />;
              return <span className="ag-desc">{row.description}</span>;
            }}
            empty={
              <div className="empty-hint">
                暂无可用工具
              </div>
            }
          />
        ) : (
          <div className="ws-tools ag-tools">
            {tools.length === 0 && (
              <div className="empty-hint">工具表为空，先检查后端工具注册是否成功</div>
            )}
            {tools.map((tool) => {
              const on = draft.tools_allowed.includes(tool.name);
              return (
                <label
                  key={tool.name}
                  className="ws-tool"
                  title={`${tool.description}\n声明归属：${tool.allowed_agents.join('、') || '（公共）'}`}
                >
                  <input
                    type="checkbox"
                    checked={on}
                    onChange={() =>
                      setDraft({
                        ...draft,
                        tools_allowed: on
                          ? draft.tools_allowed.filter((t) => t !== tool.name)
                          : [...draft.tools_allowed, tool.name],
                      })
                    }
                  />
                  <span className="ws-tool__name">{capabilityTitle(tool)}</span>
                  <code className="ws-tool__code">{tool.name}</code>
                  <span className={`ws-tool__danger ws-tool__danger--${tool.danger_level}`}>
                    {DANGER_LABELS[tool.danger_level] ?? tool.danger_level}
                  </span>
                </label>
              );
            })}
          </div>
        )}
      </Section>

      {!isNew && (
        <Section title="注入原文">
          <details className="fold">
            <summary
              className="fold__summary"
              title="不含运行时叠加的助手人设、长期记忆与角色卡"
            >
              身份段原文
            </summary>
            <div className="fold__body">
              {prompt ? (
                <>
                  <div className="stat-strip">
                    <span>
                      身份段 <b>{prompt.chars}</b> 字符
                    </span>
                  </div>
                  <pre className="mem-block" title="仅为已保存的配置">
                    {prompt.identity}
                  </pre>
                </>
              ) : (
                <div className="empty-hint">
                  <span className="spinner" /> 正在读取…
                </div>
              )}
            </div>
          </details>
        </Section>
      )}

      <div className="set-toolbar">
        <span className="set-toolbar__meta">{dirty ? '有未保存的修改' : ''}</span>
        <button
          type="button"
          className="btn btn--sm btn--primary"
          onClick={() => void onSave()}
          disabled={!canSave}
          title={
            !nameOk
              ? '标识只能用小写字母、数字与下划线，需以字母开头，长度 2-32'
              : !tempOk
                ? '温度要填 0-2 的数字'
                : !stepsOk
                  ? '步数要填 1-50 的整数'
                  : dirty
                    ? '保存修改'
                    : '没有未保存的修改'
          }
        >
          {saving ? <span className="spinner" /> : <Check size={13} />}
          {isNew ? ' 创建' : ' 保存'}
        </button>
        {!isNew && base.builtin && (
          <button
            type="button"
            className="btn btn--sm btn--ghost"
            onClick={() => void onReset()}
            disabled={saving}
            title="恢复这个专员在代码里的默认配置"
          >
            <RotateCcw size={13} /> 恢复默认
          </button>
        )}
        {!isNew && !base.builtin && (
          <button
            type="button"
            className="btn btn--sm btn--danger"
            onClick={() => void onDelete()}
            disabled={saving}
            title="删除这个自定义专员"
          >
            <Trash size={13} /> 删除
          </button>
        )}
      </div>
    </div>
  );
}

function DangerTag({ level }: { level: ToolInfo['danger_level'] }) {
  return <span className={`ws-tool__danger ws-tool__danger--${level}`}>{DANGER_LABELS[level] ?? level}</span>;
}

export default AgentEditor;
