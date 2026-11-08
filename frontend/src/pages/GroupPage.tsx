import { confirmAction } from '@/stores/confirmStore';
/** 工作群只展示公开交流和协作状态；执行明细与操作确认放在员工私聊。 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';

import {
  Plus,
  MessageSquare,
  ChevronDown,
  Send,
  Users,
  X,
} from '@/components/icons';

import { BoardPanel } from '@/components/run/BoardPanel';
import { GuardrailNotice } from '@/components/run/GuardrailNotice';
import { QuestionCard } from '@/components/QuestionCard';
import { WorkspaceShell } from '@/components/workspace/WorkspaceShell';
import { EmployeeAvatar } from '@/components/workspace/EmployeeAvatar';
import { WorkspaceRunDetails } from '@/components/workspace/WorkspaceRunDetails';
import { GroupMessageItem } from '@/components/workspace/GroupMessageItem';
import { shortModelName } from '@/lib/api';
import { fetchEmployees, groupStreamUrl, reviewWorkspaceCard, resumeWorkspaceRun } from '@/lib/api';
import { parseSSE } from '@/lib/sse';
import { useWorkspaceStore } from '@/stores/workspaceStore';
import type { Employee, GroupMessage, GroupStreamEvent } from '@/types';

/** 断线重连的退避：上限 15s —— 协作可能跑几十分钟，不能一直 1s 猛撞后端 */
const RECONNECT_MIN_MS = 1000;
const RECONNECT_MAX_MS = 15000;

export function GroupPage() {
  const navigate = useNavigate();
  const { groupId = '' } = useParams();
  const {
    currentGroup,
    run,
    messages,
    orchestrateBusy,
    activeRunId,
    cards,
    pendingApprovals,
    pendingQuestions,
    guardrails,
    resetGroupView,
    openGroup,
    applyGroupEvent,
    dismissGuardrail,
    backfillMessages,
    orchestrate,
    cancelRun,
    addMember,
    removeMember,
    resolveQuestion,
    error,
  } = useWorkspaceStore();

  const [draft, setDraft] = useState('');
  const [recipientId, setRecipientId] = useState('');
  const [replyTo, setReplyTo] = useState<GroupMessage | null>(null);
  const [intent, setIntent] = useState<'auto' | 'message' | 'task'>('auto');
  const [candidates, setCandidates] = useState<Employee[]>([]);
  const [addId, setAddId] = useState('');
  const [showTeam, setShowTeam] = useState(() => typeof window !== 'undefined' && window.innerWidth >= 1400);
  const [sendError, setSendError] = useState('');
  const scrollRef = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);
  useEffect(() => {
    if (stickToBottom.current) scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages, orchestrateBusy]);

  // 最新一次的 store 写方法收进 ref：SSE 循环是长驻协程，闭包里抓住旧的
  // `applyGroupEvent` 会让重连后的事件全打进一个过期的 reducer（Zustand 的 action
  // 本身稳定，但这条循环跨多次渲染，抓 ref 比抓依赖数组更不容易出错）。
  const handlers = useRef({ applyGroupEvent, backfillMessages });
  handlers.current = { applyGroupEvent, backfillMessages };

  // ★ 打开群组 + 订阅群组 SSE（先快照后增量）+ 断线退避重连 + 游标补拉
  // 抄的是 `SubtaskPanel.tsx:110-157` 那条已被验证的纪律：SSE 负责「快」，
  // REST 快照负责「稳」；断开时静默重试，因为真相源在库里不在流上。
  useEffect(() => {
    // ★ 同步清一次，再启动 REST 与 SSE —— 顺序反了会盖掉先到的快照
    // （`openGroup` 里有个 await，它落地时把已经写进 store 的那一屏抹掉）。
    resetGroupView();
    void openGroup(groupId);
    let cancelled = false;
    let controller: AbortController | null = null;
    let timer: number | null = null;
    let delay = RECONNECT_MIN_MS;

    const connect = async () => {
      if (cancelled) return;
      controller = new AbortController();
      try {
        const resp = await fetch(groupStreamUrl(groupId), {
          signal: controller.signal,
          headers: { Accept: 'text/event-stream' },
        });
        if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);
        for await (const event of parseSSE<GroupStreamEvent>(resp)) {
          if (cancelled) break;
          handlers.current.applyGroupEvent(event);
          // 收到任何事件都说明通道是通的 —— 退避复位，下次断开从 1s 重新开始
          delay = RECONNECT_MIN_MS;
        }
        // 服务端正常收尾也算断开：不重连的话界面会永远停在最后一帧
        throw new Error('stream closed');
      } catch (e) {
        if (cancelled) return;
        // 卸载时的 `controller.abort()` 也走到这里，所以只在**还挂着**的时候报一句。
        // 不能静默：reducer 里抛的异常与后端真断线在这一层长得一模一样，
        // 静默的话下一个查「看板不动」的人会从网络开始查起。
        console.warn('[group sse] 连接中断，退避重连', e);
        // 先按游标补拉断连窗口里漏掉的消息（`latestSeq` 为 0 时退回全量快照），
        // 再退避重连。顺序反了会让补拉的数据被随后到达的旧快照覆盖掉一半语义。
        void handlers.current.backfillMessages(groupId);
        timer = window.setTimeout(() => void connect(), delay);
        delay = Math.min(delay * 2, RECONNECT_MAX_MS);
      }
    };
    void connect();

    // 切回前台：后台标签的流可能被浏览器掐掉，回来先对一次账
    const onVisible = () => {
      if (document.visibilityState === 'visible') void handlers.current.backfillMessages(groupId);
    };
    document.addEventListener('visibilitychange', onVisible);

    return () => {
      cancelled = true;
      if (timer !== null) clearTimeout(timer);
      controller?.abort();
      document.removeEventListener('visibilitychange', onVisible);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [groupId]);

  // 拉候选员工（用于「拉入成员」下拉，也给任务板把 owner id 翻成名字）
  useEffect(() => {
    if (!currentGroup || currentGroup.id !== groupId) return;
    void fetchEmployees(currentGroup.workspace_id)
      .then((resp) => setCandidates(resp.employees))
      .catch(() => setCandidates([]));
  }, [currentGroup, groupId]);

  const memberIds = useMemo(
    () => new Set((currentGroup?.members ?? []).map((m) => m.id)),
    [currentGroup],
  );
  const addable = candidates.filter((e) => !memberIds.has(e.id));

  const doneCards = useMemo(() => cards.filter((c) => c.status === 'done').length, [cards]);

  const active = !!activeRunId || orchestrateBusy;
  const submit = async () => {
    const text = draft.trim();
    if (!text || orchestrateBusy || !currentGroup?.members?.some(member => member.enabled)) return;
    setDraft(''); setSendError(''); stickToBottom.current = true;
    await orchestrate(groupId, text, { recipient_employee_id: recipientId, reply_to: replyTo?.id, intent });
    if (!useWorkspaceStore.getState().error) setReplyTo(null);
    if (useWorkspaceStore.getState().error) { setDraft(text); setSendError(useWorkspaceStore.getState().error || '发送失败'); }
  };

  const onAddMember = () => {
    if (!addId) return;
    void addMember(groupId, addId).then(() => setAddId(''));
  };

  const openWorkbench = useCallback(
    (employeeId: string) => navigate(`/workspace/employees/${encodeURIComponent(employeeId)}`),
    [navigate],
  );

  const members = currentGroup?.members || [];
  const workingIds = new Set(cards.filter(card => card.status === 'in_progress').map(card => card.owner_employee_id));
  const progress = active
    ? pendingApprovals.length
      ? '等待确认'
      : pendingQuestions.length
        ? '等待你回答'
        : workingIds.size
          ? `${workingIds.size} 位同事正在处理`
          : '团队正在讨论'
    : pendingQuestions.length
      ? '等待你回答'
      : run?.status_label || '工作群聊';

  return <WorkspaceShell workspaceId={currentGroup?.workspace_id}>{addEmployee => <div className="main group-page team-chat">
    <header className="team-chat__header">
      <span className="team-chat__group-icon"><MessageSquare size={21} /></span>
      <div className="team-chat__heading"><h1>{currentGroup?.name || '工作群'}</h1><p><span className={`workspace-presence${active ? ' is-working' : ''}`} />{progress}<span> · {members.length} 位成员</span></p></div>
      <div className="team-chat__header-actions"><div className="team-avatar-stack">{members.slice(0, 3).map(member => <EmployeeAvatar key={member.id} name={member.name} size="small" />)}</div>
        <button type="button" className={`btn btn--sm btn--ghost${showTeam ? ' is-active' : ''}`} aria-label="查看群成员和任务" aria-expanded={showTeam} onClick={() => setShowTeam(value => !value)}><Users size={16} /><span>成员</span></button>
      </div>
    </header>
    <div className={`team-chat__layout${showTeam ? ' team-chat__layout--expanded' : ''}`}>
      <div className="team-chat__conversation">
        {active && <div className="team-run-strip" role="status"><span className="team-working-dots"><i /><i /><i /></span><span>{progress}</span>{cards.length > 0 && <span className="team-run-strip__count">{doneCards}/{cards.length} 已完成</span>}<button type="button" onClick={() => void cancelRun()} disabled={!activeRunId}>停止</button></div>}
        <div className="team-chat__scroll" ref={scrollRef} onScroll={event => { const el = event.currentTarget; stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 100; }}>
          <div className="team-chat__messages">
            {(error || sendError) && <div className="workspace-error" role="alert">{sendError || error}</div>}
            {run?.resume_available && <div className="team-approval-notice"><span>有任务可从检查点继续</span><button type="button" className="btn btn--sm" onClick={async () => { try { await resumeWorkspaceRun(run.run_id); } catch (e) { setSendError(String(e)); } }}>继续协作</button></div>}
            {run?.budget?.error && <div className="workspace-error" role="status">{run.budget.error}</div>}
            <GuardrailNotice notices={guardrails} onDismiss={dismissGuardrail} />
            {pendingApprovals.map(approval => <div className="team-approval-notice" key={`${approval.employee_id}:${approval.call_id}`}><span>{approval.requestor || '同事'}有一项操作需要你确认</span><button type="button" className="btn btn--sm" onClick={() => openWorkbench(approval.employee_id)}>前往确认</button></div>)}
            {pendingQuestions.map(question => (
              <QuestionCard
                key={question.questionId}
                asked={question}
                onSubmit={answers => resolveQuestion(question.questionId, answers)}
                onSkip={() => resolveQuestion(question.questionId, {}, true)}
              />
            ))}
            {messages.length === 0 ? <div className="team-chat__empty">
              <div className="team-empty-avatars">{members.length ? members.slice(0, 3).map(member => <EmployeeAvatar key={member.id} name={member.name} size="large" />) : <span className="workspace-welcome__icon"><Users size={32} /></span>}</div>
              <h2>{members.length ? '团队已就位' : '把同事邀请进来'}</h2>
              <p>{members.length ? '说说你想做的事，大家一起解决。' : '选好职责与模型，就能一起开工。'}</p>
              {!members.length ? <button type="button" className="btn btn--primary" onClick={addEmployee}><Plus size={15} />添加数字同事</button> : <div className="team-chat__suggestions">{['一起梳理一个新项目', '帮我调研并比较几个方案', '把这个想法变成行动计划'].map(text => <button type="button" key={text} onClick={() => setDraft(text)}>{text}<span>↗</span></button>)}</div>}
            </div> : <div className="team-chat__timeline">{messages.map((message, index) => <div key={message.id}>
              {(index === 0 || message.created_at.slice(0, 10) !== messages[index - 1].created_at.slice(0, 10)) && <div className="team-chat__date">{new Date(message.created_at).toLocaleDateString('zh-CN', { month: 'long', day: 'numeric' })}</div>}
              <GroupMessageItem message={message} onEmployee={openWorkbench} onReply={message => { setReplyTo(message); setRecipientId(message.employee_id || ""); }} />
            </div>)}</div>}
          </div>
        </div>
        <footer className="team-chat__composer">
          {replyTo && <div className="team-reply-context">回复 {replyTo.employee_name || '同事'}：{replyTo.content.slice(0, 80)}<button type="button" aria-label="取消回复" onClick={() => setReplyTo(null)}>×</button></div>}
          <form onSubmit={event => { event.preventDefault(); void submit(); }}>
          <textarea aria-label="发送到工作群" maxLength={4000} value={draft} onChange={event => setDraft(event.target.value)} placeholder={members.some(member => member.enabled) ? `发消息到 ${currentGroup?.name || '工作群'}…` : '先添加一位数字同事…'} rows={2} disabled={!members.some(member => member.enabled)} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void submit(); } }} />
          <div className="team-chat__composer-actions"><span className="team-message-options"><select aria-label="消息收件人" value={recipientId} onChange={e => setRecipientId(e.target.value)}><option value="">工作群</option>{members.filter(m => m.enabled).map(m => <option key={m.id} value={m.id}>@{m.name}</option>)}</select><select aria-label="消息类型" value={intent} onChange={e => setIntent(e.target.value as typeof intent)}><option value="auto">自动</option><option value="message">交流</option><option value="task">新任务</option></select></span><button type="submit" className="team-send" disabled={!draft.trim() || orchestrateBusy || !members.some(member => member.enabled)} aria-label="发送消息"><Send size={17} /></button></div>
        </form></footer>
      </div>
      <aside className="team-chat__details" aria-label="群成员与任务">
        <div className="team-details__title"><h2>群成员 <span>{members.length}</span></h2><button type="button" title="添加同事" aria-label="添加同事" onClick={addEmployee}><Plus size={16} /></button></div>
        <div className="team-roster">{members.map(member => <div key={member.id} className="team-roster__item"><button type="button" className="team-roster__person" onClick={() => openWorkbench(member.id)}><EmployeeAvatar name={member.name} /><span><strong>{member.name}</strong><small>{member.tagline || '数字同事'}</small><code>{shortModelName(member.model || '默认模型')}</code></span><span className={`team-roster__status${workingIds.has(member.id) ? ' is-working' : ''}`}>{!member.enabled ? '停用' : workingIds.has(member.id) ? '工作中' : '就绪'}</span></button><button type="button" className="team-roster__remove" title={`将${member.name}移出群聊`} aria-label={`将${member.name}移出群聊`} disabled={active} onClick={async () => { if (await confirmAction({ title: `将${member.name}移出群聊？`, description: "移出后，这位同事将不再参与此群聊。", confirmLabel: "移出群聊", tone: "warning" })) void removeMember(groupId, member.id); }}><X size={12} /></button></div>)}</div>
        {addable.length > 0 && <div className="team-roster__add"><select aria-label="选择群成员" value={addId} onChange={event => setAddId(event.target.value)}><option value="">邀请已有同事…</option>{addable.map(employee => <option value={employee.id} key={employee.id}>{employee.name}</option>)}</select><button type="button" className="btn btn--sm btn--ghost" onClick={onAddMember} disabled={!addId || active}><Plus size={14} /></button></div>}
        {cards.length > 0 && <div className="team-details__tasks"><div className="team-details__title"><h2>团队任务 <span>{doneCards}/{cards.length}</span></h2><ChevronDown size={14} /></div><BoardPanel cards={cards} employees={candidates} onGoToEmployee={openWorkbench} onReview={async (cardId, accepted, reason) => { const response = await reviewWorkspaceCard(cardId, accepted, reason); applyGroupEvent({ type: 'card_status', card: response.card, previous_status: 'review' }); }} /></div>}
        <WorkspaceRunDetails run={run} revision={cards.map(c => c.updated_at).join("|")} employeeId={members[0]?.id || ""} />
        {!members.length && <button type="button" className="workspace-invite" onClick={addEmployee}><Plus size={15} />邀请第一位同事</button>}
      </aside>
    </div>
  </div>}</WorkspaceShell>;
}
