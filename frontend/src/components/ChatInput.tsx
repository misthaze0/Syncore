/**
 * 输入区（2026-10 新版布局）
 *
 * 结构是「一张卡片 + 卡片外的一条托盘」：
 * - 卡片：正文（textarea）+ 卡片内底部一行 —— 左「+」，右「模型 / 麦克风 / 发送」
 * - 托盘（卡片外）：工作区、本轮权限、快捷键提示与字数
 * Enter 发送、Shift + Enter 换行；自动增高（最高 180px）；流式中按钮变「停止」；
 * 麦克风按住说话，松开把转写文字填进输入框（不直接发送，由用户确认）；
 * 外部可用 ref 写入文本（「编辑消息」）。
 *
 * ★ 「联网」「深度思考」为什么收进 `+` 菜单
 * 它们一年里被改动的次数远小于被看到的次数：默认值就是绝大多数轮次的答案，
 * 常驻一行等于让所有人一直陪着一个不动的开关。收进 `+` 之后仍然两下点到，
 * 而托盘那一行留给了**每段对话都要确认**的事（在哪个目录里干活）。
 * 开关不在场，状态不能因此不可见 —— 「与默认值不同」时 `+` 会亮，
 * 悬停说出差异（「已关闭联网」），点进去用勾选态核对。
 *
 * ★ 对话附件（P0-1）
 * 「+」菜单 / 整块拖拽 / 直接粘贴，三条路进同一个附件队列：
 * 每个文件立即上传到 `/files/attach`（临时上下文，**不入知识库**），
 * chip 状态 流式中 → 就绪 / 失败。失败不阻塞别的附件，也不能被发出。
 *
 * ★ 快捷指令（问法库）
 * 输入框敲 `/` 唤起快捷指令菜单（可搜索、键盘上下选），选中后把问法**整段填进输入框**；
 * 用户还能接着改、带附件、补上下文。它不是工作流的别名 —— 这里不执行任何东西。
 * 管理只在「设置 → 快捷指令」一处；输入框和命令面板读同一份清单。
 */

import { forwardRef, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState } from 'react';

import {
  ArrowUp,
  Brain,
  Check,
  ChevronDown,
  FileText,
  Globe,
  Loader2,
  Mic,
  Paperclip,
  Plus,
  Settings,
  Sparkles,
  Square,
  Zap,
} from '@/components/icons';

// 卡片的玫瑰荆棘挂在 <html data-composer> 上；引进 store 只为了让「有这张卡」必然「属性已写好」
import '@/hooks/useComposerStyle';
import { useVoiceInput } from '@/hooks/useVoiceInput';
import { useT } from '@/i18n';
import { attachFile, createQuickCommand, fetchQuickCommands, shortModelName } from '@/lib/api';
import { TurnModePicker } from '@/components/TurnModePicker';
import { WorkdirPicker } from '@/components/WorkdirPicker';
import { useAgentStore } from '@/stores/agentStore';
import { toast } from '@/stores/toastStore';
import { emptyDraftAttachments, useChatDraftStore } from '@/stores/chatDraftStore';
import type { PendingAttachment } from '@/stores/chatDraftStore';
import type {
  AttachmentMeta,
  ModelOption,
  PermissionModeInfo,
  PermissionModeValue,
  QuickCommand,
} from '@/types';

interface ChatInputProps {
  onSend: (text: string, attachments?: AttachmentMeta[]) => void;
  onStop: () => void;
  isStreaming: boolean;
  disabled?: boolean;
  placeholder?: string;
  /**
   * 当前会话 id —— 工作区绑定记在它名下（见 `app/workdir`）。
   * 输入区里唯一需要「这是哪段对话」的东西就是这颗胶囊。
   */
  threadId: string;
  /**
   * 是否允许联网搜索。现在收在「+」菜单里，不再占托盘一行。
   * 默认开 —— 「能自动的不要问」，用户只有明确不想联网时才关。
   */
  webEnabled?: boolean;
  /** 切换联网开关。不传则菜单里不渲染这一项（组件保持可单独使用） */
  onToggleWeb?: (value: boolean) => void;
  /** 深度思考开关的状态（默认关 —— 思考会明显增加首字延迟与 token 消耗） */
  thinkingEnabled?: boolean;
  /** 切换深度思考开关。不传则不渲染 */
  onToggleThinking?: (value: boolean) => void;
  /**
   * 本轮权限档位（空串 = 跟随设置里的默认档）。
   *
   * 只有**收紧**的候选会出现在这里 —— 放宽发生在设置页，一次点击，
   * 而不是输入框旁边一个手滑就能改到的下拉。
   */
  turnMode?: PermissionModeValue | '';
  /** 可收紧的档位；空数组或未传则不渲染这个选择器 */
  turnModeOptions?: PermissionModeInfo[];
  /** 设置里默认档的人话名字，用在「跟随设置」那一项 */
  defaultModeLabel?: string;
  /** 切换本轮档位。不传则不渲染 */
  onTurnModeChange?: (mode: PermissionModeValue | '') => void;
  /**
   * 可选模型 —— 各家供应商测试连接成功并保存过的那几条（含当前生效的一条）。
   * 每条都带着自己的供应商与 profile，选它 = 连连接参数一起切过去。
   * 只有一条也照常渲染这颗钮：它同时是通往模型管理的入口。
   */
  models?: ModelOption[];
  /** 切换模型 = 直接把它设为全局默认（对所有对话生效）。不传则不渲染模型钮 */
  onModelChange?: (model: ModelOption) => void;
  /** 打开设置中心（传页签 key）。不传则弹层里没有「模型管理」那一项 */
  onOpenSettings?: (tab?: string) => void;
}

export interface ChatInputRef {
  /** 设置输入框文本并聚焦 */
  setText: (text: string) => void;
}

const MAX_HEIGHT = 180;
/** 与后端 ChatRequest.attachments 的 max_length 一致 */
const MAX_ATTACHMENTS = 5;

/** 与后端 app.rag.parser.SUPPORTED_SUFFIXES 同源 —— 白名单两边必须一致 */
const ACCEPT =
  '.md,.markdown,.txt,.csv,.log,.pdf,.docx,.xlsx,.pptx,.py,.js,.ts,.json,.yaml,.yml';

/** 输入框里的一个附件（上传中的本地条目 → 成功换成元数据 / 失败带原因） */

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export const ChatInput = forwardRef<ChatInputRef, ChatInputProps>(function ChatInput(
  {
    onSend,
    onStop,
    isStreaming,
    disabled,
    placeholder,
    threadId,
    webEnabled = true,
    onToggleWeb,
    thinkingEnabled = false,
    onToggleThinking,
    turnMode = '',
    turnModeOptions,
    defaultModeLabel = '',
    onTurnModeChange,
    models,
    onModelChange,
    onOpenSettings,
  },
  ref,
) {
  const value = useChatDraftStore(s => s.drafts[threadId]?.text ?? '');
  const setValue = useCallback((update: string | ((previous: string) => string)) => useChatDraftStore.getState().setText(threadId, update), [threadId]);
  const t = useT();
  const pending = useChatDraftStore(s => s.drafts[threadId]?.attachments ?? emptyDraftAttachments);
  const setPending = useCallback((update: PendingAttachment[] | ((previous: PendingAttachment[]) => PendingAttachment[])) => useChatDraftStore.getState().setAttachments(threadId, update), [threadId]);
  const [dragOver, setDragOver] = useState(false);
  // 「+」菜单（附件 / 深度思考 / 联网）与模型菜单：收起来的那几样各一个浮层
  const [addOpen, setAddOpen] = useState(false);
  const [modelOpen, setModelOpen] = useState(false);
  // `/` 快捷指令菜单：query 是 `/` 后面那段文字（用于过滤）
  const [slashQuery, setSlashQuery] = useState<string | null>(null);
  const [commands, setCommands] = useState<QuickCommand[]>([]);
  const [slashIndex, setSlashIndex] = useState(0);
  const [slashLoading, setSlashLoading] = useState(false);
  const [slashError, setSlashError] = useState(false);
  const [savingCommand, setSavingCommand] = useState(false);

  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const slashRef = useRef<HTMLDivElement>(null);
  const addRef = useRef<HTMLDivElement>(null);
  const modelRef = useRef<HTMLDivElement>(null);

  // 语音转写只落到草稿里：用户先看、先改，发不发由他自己按发送。
  // 有草稿时接在后面（并补一个空格），不能把已经写好的话顶掉。
  const insertTranscript = useCallback((text: string) => {
    setValue((prev) => {
      if (!prev) return text;
      return /\s$/.test(prev) ? `${prev}${text}` : `${prev} ${text}`;
    });
    window.setTimeout(() => {
      const el = textareaRef.current;
      if (!el) return;
      el.focus();
      el.selectionStart = el.selectionEnd = el.value.length;
    }, 0);
  }, [setValue]);

  const voice = useVoiceInput(insertTranscript);

  // 「+」与模型菜单：点外面收起，Esc 也收起（与顶栏、工作区选择器同一套手感）。
  // 两个浮层不会同时开着 —— 点一个必然先关掉另一个，所以一次判断两层的容器就够。
  useEffect(() => {
    if (!addOpen && !modelOpen) return;
    const onDown = (e: MouseEvent) => {
      const target = e.target as Node;
      if (addRef.current?.contains(target) || modelRef.current?.contains(target)) return;
      setAddOpen(false);
      setModelOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setAddOpen(false);
        setModelOpen(false);
      }
    };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [addOpen, modelOpen]);

  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    // 空输入时不读 scrollHeight：Chrome 会按**占位文案**换行后的行数来算它，
    // 于是一句话都没写的输入框会长成三行高。占位文案是提示，不是内容。
    if (!value) {
      el.style.height = '';
      return;
    }
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, MAX_HEIGHT)}px`;
  }, [value]);

  // 「编辑消息」从外部把原文填回输入框 —— 丢了它，消息编辑功能就断了
  useImperativeHandle(ref, () => ({
    setText: (text: string) => {
      setValue(text);
      // 下一帧聚焦并放到末尾
      window.setTimeout(() => {
        const el = textareaRef.current;
        if (!el) return;
        el.focus();
        el.selectionStart = el.selectionEnd = el.value.length;
      }, 0);
    },
  }));

  // 每次唤起 `/` 菜单都重拉一次指令：设置页刚改完的指令要能立刻敲得到。
  // 用 ref 记住"这次已经拉过"，否则每敲一个字符都会再请求一次。
  const slashFetched = useRef(false);
  useEffect(() => {
    if (slashQuery === null) {
      slashFetched.current = false;
      return;
    }
    if (slashFetched.current) return;
    slashFetched.current = true;
    setSlashLoading(true);
    setSlashError(false);
    void fetchQuickCommands()
      .then((res) => setCommands(res.commands))
      .catch(() => setSlashError(true))
      .finally(() => setSlashLoading(false));
  }, [slashQuery]);

  const slashOpen = slashQuery !== null;
  /**
   * 过滤：按 `/` 后的关键词做子串匹配（标题 / 分类 / 正文都算）。
   * 指令量级在几十条，子串够用 —— 与命令面板同一条取舍，不引模糊搜索库。
   */
  const slashResults =
    slashQuery === null
      ? []
      : commands.filter((c) => {
          const q = slashQuery.trim().toLowerCase();
          if (!q) return true;
          return (
            c.title.toLowerCase().includes(q) ||
            c.category.toLowerCase().includes(q) ||
            c.body.toLowerCase().includes(q)
          );
        });

  useEffect(() => setSlashIndex(0), [slashQuery]);

  const closeSlash = useCallback(() => setSlashQuery(null), []);

  const applyCommand = useCallback(
    (command: QuickCommand) => {
      // 整段问法填进去（连同 `{{...}}` 占位），由用户自己改
      setValue(command.body);
      closeSlash();
      window.setTimeout(() => {
        const el = textareaRef.current;
        if (!el) return;
        el.focus();
        el.selectionStart = el.selectionEnd = el.value.length;
      }, 0);
    },
    [closeSlash, setValue],
  );

  // 「存为快捷指令」：当前草稿就是正文，标题取第一行前 20 字。
  // 零门槛保存；想改标题 / 分类去设置页 —— 不在这里再弹一个表单。
  const saveDraftAsCommand = useCallback(async () => {
    const body = value.trim();
    if (!body || savingCommand) return;
    const title = body.split('\n')[0].trim().slice(0, 20) || '未命名指令';
    setSavingCommand(true);
    try {
      const created = await createQuickCommand({ title, body, category: '通用' });
      setCommands((prev) => (prev.some((c) => c.id === created.id) ? prev : [...prev, created]));
      setAddOpen(false);
      toast(`已存为快捷指令「${title}」`, 'success');
    } catch (e) {
      toast(e instanceof Error ? e.message : '保存失败', 'error');
    } finally {
      setSavingCommand(false);
    }
  }, [value, savingCommand]);

  // ---------- 附件 ----------

  const addFiles = useCallback(
    (files: FileList | File[] | null) => {
      if (!files) return;
      const list = Array.from(files);
      if (!list.length) return;

      const room = MAX_ATTACHMENTS - pending.filter((p) => p.status !== 'error').length;
      if (room <= 0) {
        toast(`一次最多带 ${MAX_ATTACHMENTS} 个附件`, 'warning');
        return;
      }
      const accepted = list.slice(0, room);
      if (accepted.length < list.length) toast(`一次最多带 ${MAX_ATTACHMENTS} 个附件`, 'warning');

      const entries: PendingAttachment[] = accepted.map((file) => ({
        localId: `${Date.now()}-${Math.random().toString(36).slice(2)}`,
        status: 'uploading' as const,
        meta: {
          attachment_id: '',
          filename: file.name,
          file_size: file.size,
          chars: 0,
          truncated: false,
        },
      }));

      setPending((prev) => [...prev, ...entries]);

      // 请求放在状态更新器外，避免 StrictMode 重放更新时重复上传。
      accepted.forEach((file, i) => {
        const localId = entries[i].localId;
        attachFile(file)
          .then((meta) => {
            setPending((cur) =>
              cur.map((p) =>
                p.localId === localId ? { ...p, status: 'ready' as const, meta } : p,
              ),
            );
          })
          .catch((e) => {
            const message = e instanceof Error ? e.message : '上传失败';
            setPending((cur) =>
              cur.map((p) =>
                p.localId === localId ? { ...p, status: 'error' as const, error: message } : p,
              ),
            );
          });
      });
    },
    [pending, setPending],
  );

  const removeAttachment = useCallback((localId: string) => {
    setPending((prev) => prev.filter((p) => p.localId !== localId));
  }, [setPending]);

  const readyMetas = pending
    .filter((p): p is PendingAttachment & { meta: AttachmentMeta } => p.status === 'ready')
    .map((p) => p.meta);

  const hasBlocked = pending.some((p) => p.status !== 'ready');

  // ---------- 发送 ----------

  const submit = useCallback(() => {
    const text = value.trim();
    if (!text || isStreaming || disabled) return;
    // 上传中的附件不能丢；失败的附件说明用户想要它 —— 都不该悄悄发出去
    if (hasBlocked) {
      toast('请等待附件上传完成，或移除上传失败的附件后再发送', 'warning');
      return;
    }
    onSend(text, readyMetas.length > 0 ? readyMetas : undefined);
    setValue('');
    setPending([]);
    closeSlash();
  }, [value, isStreaming, disabled, hasBlocked, readyMetas, onSend, closeSlash, setValue, setPending]);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    // ---------- `/` 快捷指令菜单的键盘语义优先于发送 ----------
    if (slashOpen) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        if (slashResults.length > 0) setSlashIndex((i) => Math.min(i + 1, slashResults.length - 1));
        return;
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault();
        setSlashIndex((i) => Math.max(i - 1, 0));
        return;
      }
      if (e.key === 'Escape') {
        e.preventDefault();
        closeSlash();
        return;
      }
      if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
        const picked = slashResults[slashIndex];
        // 有候选就填入；没有候选时把 Enter 还给「发送」，否则一个字都发不出去
        if (picked) {
          e.preventDefault();
          applyCommand(picked);
          return;
        }
      }
    }

    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit();
    }
  };

  const handleChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const next = e.target.value;
    setValue(next);
    // `/` 只在「输入以 / 开头、且后面没有空格」时唤起 ——
    // 一旦开始写正文（出现空格 / 换行），菜单就该收起，把位置让给输入。
    const match = next.match(/^\/(\S*)$/);
    setSlashQuery(match ? match[1] : null);
  };

  const handlePaste = (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    // 粘贴的文件与文字互不干扰：有文件就走附件（截图会被后端诚实拒绝）
    const files = e.clipboardData?.files;
    if (files?.length) {
      e.preventDefault();
      addFiles(files);
    }
  };

  // ---------- 新版界面的派生值 ----------

  /** 「+」里现在有多少项**与默认值不同** —— 决定那颗钮要不要亮起来 */
  const deviations = useMemo(() => {
    const out: string[] = [];
    if (onToggleThinking && thinkingEnabled) out.push(t('input.thinking'));
    // 联网的默认是「开」，所以只有被关掉才算偏离
    if (onToggleWeb && !webEnabled) out.push(t('input.webOffShort'));
    return out;
  }, [onToggleThinking, thinkingEnabled, onToggleWeb, webEnabled, t]);

  /**
   * 模型钮。只有一个候选也渲染 —— 它同时是通往「模型管理」的入口，
   * 藏起来会让人以为「这产品不能换模型」。
   * 选谁 = 谁成为全局默认（后端 `LLM_MODEL`），没有「只覆盖这一句」。
   */
  const modelOptions = models ?? [];
  const showModel = Boolean(onModelChange) && modelOptions.length > 0;
  const serviceInfo = useAgentStore((s) => s.serviceInfo);
  /** 候选清单为空时，钮上至少还能显示当前生效的模型名（来自 /info） */
  const activeModel = modelOptions.find((m) => m.default);
  const activeModelLabel =
    activeModel?.label || (serviceInfo?.model ? shortModelName(serviceInfo.model) : '') || t('input.modelUnknown');

  /**
   * 「深度思考」开关在**当前模型**上的可用性（后端按模型 × 网关判定）。
   * 拿不到能力（老后端 / 降级清单）时按「可用」处理 —— 不因此禁用开关。
   * 三种态：正常切换 / 锁在开（仅思考模型，关不掉）/ 不可用（无思考模型）。
   */
  const activeThinking = activeModel?.thinking;
  const thinkingLockedOn = Boolean(activeThinking && thinkingEnabled && !activeThinking.can_off);
  const thinkingUnavailable = Boolean(activeThinking && !thinkingEnabled && !activeThinking.can_on);
  const thinkingDisabled = thinkingLockedOn || thinkingUnavailable;
  const thinkingTitle = thinkingLockedOn
    ? activeThinking?.reason || '当前模型始终深度思考，无法关闭'
    : thinkingUnavailable
      ? activeThinking?.reason || '当前模型不支持深度思考'
      : t('input.thinkingHint');

  /**
   * 按供应商分组。模型名可能重名（两家都有 `qwen-plus`），
   * 分组后才看得出「点这一条会连到哪台网关」。
   */
  const modelGroups = useMemo(() => {
    const groups = new Map<string, ModelOption[]>();
    for (const m of modelOptions) {
      const key = m.provider || '';
      const list = groups.get(key);
      if (list) list.push(m);
      else groups.set(key, [m]);
    }
    return Array.from(groups, ([provider, items]) => ({ provider, items }));
  }, [modelOptions]);

  const listening = voice.state === 'listening';
  const processing = voice.busy;

  return (
    <div
      className={`composer${dragOver ? ' composer--dragover' : ''}`}
      onDragOver={(e) => {
        e.preventDefault();
        setDragOver(true);
      }}
      onDragLeave={() => setDragOver(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDragOver(false);
        addFiles(e.dataTransfer?.files ?? null);
      }}
    >
      {/* ---------- 附件 chips ---------- */}
      {pending.length > 0 && (
        <div className="composer__attachments">
          {pending.map((p) => (
            <span
              className={`attachment-chip attachment-chip--${p.status}`}
              key={p.localId}
              title={
                p.status === 'error'
                  ? p.error
                  : p.status === 'ready'
                    ? `已就绪 · 读取了 ${p.meta!.chars} 字${p.meta!.truncated ? '（过长已截断）' : ''}`
                    : '正在读取文件内容…'
              }
            >
              {p.status === 'uploading' ? (
                <Loader2 size={11} className="spin" />
              ) : p.status === 'error' ? (
                <Square size={8} fill="currentColor" />
              ) : (
                <FileText size={11} />
              )}
              <span className="attachment-chip__name">{p.meta!.filename}</span>
              <span className="attachment-chip__meta">
                {p.status === 'error' ? (p.error ?? '失败') : formatSize(p.meta!.file_size)}
              </span>
              <button
                type="button"
                className="attachment-chip__remove"
                onClick={() => removeAttachment(p.localId)}
                aria-label="移除附件"
              >
                ×
              </button>
            </span>
          ))}
        </div>
      )}

      {/* ---------- `/` 快捷指令菜单 ---------- */}
      {slashOpen && (
        <div className="slash" ref={slashRef}>
          <div className="slash__list" role="listbox">
            {slashResults.length === 0 ? (
              <div className="slash__empty">
                {slashLoading ? '正在读取…' : slashError ? '快捷指令读取失败' : '没有匹配的快捷指令'}
              </div>
            ) : (
              slashResults.map((c, i) => (
                <div
                  key={c.id}
                  role="option"
                  aria-selected={i === slashIndex}
                  className={`slash__item${i === slashIndex ? ' slash__item--active' : ''}`}
                  onMouseEnter={() => setSlashIndex(i)}
                  onClick={() => applyCommand(c)}
                >
                  <span className="slash__title">{c.title}</span>
                  <span className="slash__body">{c.body}</span>
                  <span className="slash__category">{c.category}</span>
                </div>
              ))
            )}
          </div>
          <div className="slash__footer">
            <span>↑↓ 选择 · Enter 填入 · Esc 关闭</span>
            {onOpenSettings && (
              <button
                type="button"
                className="slash__manage"
                onClick={() => {
                  closeSlash();
                  onOpenSettings('quickcmd');
                }}
              >
                管理快捷指令
              </button>
            )}
          </div>
        </div>
      )}

      {/*
        ---------- 卡片：正文 + 卡内底部一行 ----------
        「+」在左、模型/麦克风/发送在右，全都在卡片**里面**（图一那版的空间分配）。
        托盘在卡片外面：那几颗胶囊是「这段对话的环境」，不是「这一句话的操作」，
        把它们塞进卡片会让人以为关掉对话重开就没了。
      */}
      <div className="composer__card">
        <textarea
          ref={textareaRef}
          className="composer__textarea"
          rows={1}
          value={value}
          placeholder={placeholder ?? t('input.placeholder')}
          onChange={handleChange}
          onKeyDown={handleKeyDown}
          onPaste={handlePaste}
          disabled={disabled}
        />

        <div className="composer__bar">
          {/* 「+」菜单：附件 + 两个开关。附件是这一行里唯一每轮都可能用的动作 */}
          <div className="composer__group" ref={addRef}>
            <button
              type="button"
              className={`composer__round composer__add${deviations.length > 0 ? ' composer__add--lit' : ''}`}
              onClick={() => setAddOpen((v) => !v)}
              aria-expanded={addOpen}
              aria-haspopup="menu"
              aria-label={t('input.more')}
              title={deviations.length > 0 ? `${t('input.more')}：${deviations.join(' · ')}` : t('input.more')}
            >
              <Plus size={17} />
            </button>

            {addOpen && (
              <div className="composer__menu" role="menu" aria-label={t('input.more')}>
                <button
                  type="button"
                  role="menuitem"
                  className="composer__menu-item"
                  disabled={disabled || isStreaming}
                  onClick={() => {
                    setAddOpen(false);
                    fileInputRef.current?.click();
                  }}
                >
                  <Paperclip size={14} />
                  <span className="composer__menu-label">{t('input.attachTitle')}</span>
                </button>

                <button
                  type="button"
                  role="menuitem"
                  className="composer__menu-item"
                  disabled={!value.trim() || savingCommand}
                  title="把输入框里的这段话存成快捷指令，下次敲 / 就能再用"
                  onClick={() => void saveDraftAsCommand()}
                >
                  {savingCommand ? <Loader2 size={14} className="spin" /> : <Zap size={14} />}
                  <span className="composer__menu-label">存为快捷指令</span>
                </button>

                {/*
                  联网开关（P1-8）：默认**开**。关掉时后端不给 `browser_agent` 绑定
                  `web_search` 工具 —— 不是「劝它别搜」，而是它根本拿不到这个工具。
                  这里只暴露「能不能联网」这一个决定；
                  top_k / 召回路数这类参数不在这里（术语不上第一屏）。
                */}
                {onToggleWeb && (
                  <button
                    type="button"
                    role="menuitemcheckbox"
                    aria-checked={webEnabled}
                    className="composer__menu-item"
                    title={t('input.webHint')}
                    onClick={() => onToggleWeb(!webEnabled)}
                  >
                    <Globe size={14} />
                    <span className="composer__menu-text">
                      <span className="composer__menu-label">{t('input.web')}</span>
                    </span>
                    {webEnabled && (
                      <span className="composer__menu-check" aria-hidden="true">
                        <Check size={13} />
                      </span>
                    )}
                  </button>
                )}
                {onToggleThinking && (
                  <button
                    type="button"
                    role="menuitemcheckbox"
                    aria-checked={thinkingEnabled}
                    className="composer__menu-item"
                    title={thinkingTitle}
                    disabled={thinkingDisabled}
                    onClick={() => onToggleThinking(!thinkingEnabled)}
                  >
                    <Brain size={14} />
                    <span className="composer__menu-text">
                      <span className="composer__menu-label">{t('input.thinking')}</span>
                    </span>
                    {thinkingEnabled && (
                      <span className="composer__menu-check" aria-hidden="true">
                        <Check size={13} />
                      </span>
                    )}
                  </button>
                )}
              </div>
            )}

            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept={ACCEPT}
              style={{ display: 'none' }}
              onChange={(e) => {
                addFiles(e.target.files);
                e.target.value = '';
              }}
            />
          </div>

          <div className="composer__group composer__group--right">
            {/*
              模型选择器：清单来自「设置 → 模型」里测试通过并保存过的连接，
              按供应商分组；在这里点谁，谁就成为全局默认（对所有对话生效），
              模型 / Base URL / Key 作为同一个连接一起切过去。
              没有 Key 的供应商、没保存过的模型**不在这里出现** ——
              列出来只会让人选到一个必然失败的端点。
            */}
            {showModel && (
              <div className="composer__group" ref={modelRef}>
                <button
                  type="button"
                  className="composer__model"
                  onClick={() => setModelOpen((v) => !v)}
                  aria-expanded={modelOpen}
                  aria-haspopup="listbox"
                  aria-label={t('chat.modelAria')}
                  title={t('chat.modelSwitch')}
                >
                  <Sparkles size={13} className="composer__model-icon" />
                  <span className="composer__model-name">{activeModelLabel}</span>
                  <ChevronDown size={12} className={`composer__model-caret${modelOpen ? ' is-open' : ''}`} />
                </button>

                {modelOpen && (
                  <div className="composer__menu composer__model-menu" role="listbox" aria-label={t('chat.modelAria')}>
                    {modelOptions.length === 0 ? (
                      /* 拉不到候选也要说句话：一片空白会被读成「这个产品不能换模型」 */
                      <div className="composer__model-empty">{t('input.modelEmpty')}</div>
                    ) : (
                      modelGroups.map((group) => (
                        <div
                          className="composer__model-group"
                          role="group"
                          aria-label={group.provider || undefined}
                          key={group.provider || '_'}
                        >
                          {group.provider && (
                            <div className="composer__model-group-label">{group.provider}</div>
                          )}
                          {group.items.map((m) => {
                            // `default` 由后端按「模型 + 连接」一起比对：
                            // 同名模型在两家出现时，只有真正生效的那条打勾
                            const picked = m.default;
                            // 思考能力角标只标「偏离」：始终思考 / 无思考。
                            // 常态（可开可关）不标 —— 每一行都挂角标等于没有角标。
                            const thinkTag = m.thinking
                              ? m.thinking.can_off === false
                                ? '始终思考'
                                : m.thinking.can_on === false
                                  ? '无思考'
                                  : ''
                              : '';
                            return (
                              <button
                                type="button"
                                role="option"
                                aria-selected={picked}
                                key={`${m.profile_id ?? ''}:${m.id}`}
                                className="composer__menu-item composer__model-row"
                                title={m.provider ? `${m.label} · ${m.provider}` : m.label}
                                onClick={() => {
                                  setModelOpen(false);
                                  if (!picked) onModelChange?.(m);
                                }}
                              >
                                {/* 小圆点标出「哪个是全局默认」—— 光看模型名分不出来 */}
                                <span
                                  className={`composer__model-dot${picked ? ' composer__model-dot--default' : ''}`}
                                  aria-hidden="true"
                                />
                                <span className="composer__menu-text">
                                  <span className="composer__menu-label">{m.label}</span>
                                </span>
                                {thinkTag && (
                                  <span className="composer__model-think" aria-hidden="true">
                                    {thinkTag}
                                  </span>
                                )}
                                {picked && (
                                  <span className="composer__menu-check" aria-hidden="true">
                                    <Check size={13} />
                                  </span>
                                )}
                              </button>
                            );
                          })}
                        </div>
                      ))
                    )}

                    {onOpenSettings && (
                      <button
                        type="button"
                        className="composer__model-manage"
                        onClick={() => {
                          setModelOpen(false);
                          onOpenSettings('models');
                        }}
                      >
                        <Settings size={13} />
                        {t('input.modelManage')}
                      </button>
                    )}
                  </div>
                )}
              </div>
            )}

            {/* 麦克风：点一下开始持续聆听，说一句转一句；再点一下（变成方块）结束 */}
            <button
              type="button"
              className={`composer__round composer__mic${listening ? ' composer__round--recording' : ''}${!listening && processing ? ' composer__round--processing' : ''}`}
              title={voice.error ?? (listening ? t('input.micStop') : processing ? t('input.micBusy') : t('input.mic'))}
              onClick={() => (listening ? voice.stop() : void voice.start())}
              disabled={disabled || (!listening && processing)}
              aria-label={listening ? t('input.micStop') : t('input.mic')}
            >
              {listening ? (
                <Square size={12} fill="currentColor" />
              ) : processing ? (
                <Loader2 size={16} className="spin" />
              ) : (
                <Mic size={16} />
              )}
            </button>

            {isStreaming ? (
              <button
                type="button"
                className="composer__round composer__round--stop"
                onClick={onStop}
                title={t('input.stop')}
                aria-label={t('input.stop')}
              >
                <Square size={13} fill="currentColor" />
              </button>
            ) : (
              <button
                type="button"
                className="composer__round composer__round--send"
                onClick={submit}
                disabled={!value.trim() || disabled}
                title={t('input.send')}
                aria-label={t('input.send')}
              >
                <ArrowUp size={18} />
              </button>
            )}
          </div>
        </div>
      </div>

      {/*
        ---------- 托盘：这段对话「在哪、以什么档」跑 ----------
        工作区是主角（每段对话各自记住），权限档位紧跟着它 ——
        两者都是「本轮之外一直生效」的环境决定，所以放在同一行。
      */}
      <div className="composer__tray">
        <WorkdirPicker threadId={threadId} />
        {onTurnModeChange && (turnModeOptions?.length ?? 0) > 0 && (
          <TurnModePicker
            value={turnMode}
            options={turnModeOptions ?? []}
            defaultLabel={defaultModeLabel}
            onChange={onTurnModeChange}
          />
        )}

        <span className="composer__tray-space" aria-hidden="true" />
        {value.length > 0 && <span className="composer__chars">{t('input.chars', { n: value.length })}</span>}
      </div>

      {voice.error && (
        <div className="composer__error" onClick={voice.clearError} title={t('common.close')}>
          {voice.error}
        </div>
      )}
    </div>
  );
});
