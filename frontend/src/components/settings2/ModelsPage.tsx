/**
 * 模型页 —— 对话模型 / 检索模型 / 语音模型，三块分段。
 *
 * ★ 对话模型：供应商目录 + 每家各自的 Key + 模型清单（参照 Cherry Studio / LobeChat 的接入范式）
 *
 * - 供应商目录来自后端 `data/llm_profiles.json`（ADR 0021 切片 1），每家各自记 Key；
 *   chip 按目录渲染，前端不再维护预设表（否则两份目录漂移）；选中态从表单 Base URL
 *   反查，不再单存一个 providerId —— 于是地址落在目录里时不会再冒出一个重复的「自定义」；
 * - 流程固定为：选供应商 → 填接入信息 → **测试连接** → 保存；
 * - 「默认模型」是**下拉**，按供应商分组：选中的供应商展示实时模型清单
 *   （POST /llm/models/list，用草稿参数 GET {base_url}/models，不落盘）+ 已保存模型；
 *   其他供应商只展示各自已保存的模型。切换供应商会把该家已保存的连接参数带进表单；
 * - 「测试连接」用**表单草稿参数**真打一次模型（POST /llm/test，后端不落盘）；
 *   空 Key 时后端按「这个 Base URL 在目录里的 Key」回退，不拿全局生效的 Key 串门；
 *   凭据类改动（Base URL / Key / 模型名）必须测试通过才允许保存；
 * - Key 输入框显示**掩码值**（掩码不是密钥）：聚焦即清空供输入新 Key，
 *   留空失焦 = 保持该供应商目录里的 Key 不变；后端 GET 不回明文；
 * - 「已保存的模型」是**按连接记录**的：只有测试通过并保存过的模型才进清单；
 *   对话页的下拉只用这份清单。选中别家的已保存模型 = 模型 / Base URL / Key /
 *   profile 作为一个连接整体切过去（没有「只换模型名」）；
 * - 保存走 `PUT /api/llm/models`（运行时覆写，不碰 backend/.env）。
 *
 * ★ 检索模型：只支持「自填 api 供应商」或「本地模型」——不提供预设。
 *   换 embedding 模型会让已入库向量作废，破坏性操作的重建入口仍在高级设置。
 *
 * ★ 语音模型：STT / TTS 的模型与音色；Key 目前跟随生效对话连接的 Key，
 *   语音独立接入配置（VOICE_BASE_URL / VOICE_API_KEY）为跟进项。
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';

import {
  Check,
  ChevronDown,
  Eye,
  EyeOff,
  ExternalLink,
  Plus,
  RefreshCw,
  Stethoscope,
  X,
  Zap,
} from '@/components/icons';

import {
  fetchLlmModel,
  fetchLlmRemoteModels,
  fetchRagModels,
  fetchVoiceSettings,
  probeRagModels,
  probeVoiceSettings,
  resetLlmModel,
  resetVoiceSettings,
  testLlmModel,
  updateLlmModel,
  updateRagModels,
  updateVoiceSettings,
} from '@/lib/api';
import { formatMs } from '@/lib/format';
import type {
  LlmModelsResponse,
  LlmProbeResult,
  LlmProfileView,
  RagModelsResponse,
  RagProbeResult,
  VoiceProbeResult,
  VoiceSettingsResponse,
} from '@/types';
import { PageHead, Row, Rows, Section } from './kit';

/* ---------- 供应商目录（对话模型） ----------
 * 目录由后端持有（`data/llm_profiles.json`，ADR 0021 切片 1）：每家各自记 Key，
 * 前端只渲染、不维护 —— 否则两份目录漂移。后端 seed 只放 OpenAI 兼容
 * （/v1/chat/completions）的供应商：链路是 ChatOpenAI，不摆填了也用不了的假选项。
 */

type ModelTab = 'chat' | 'retrieval' | 'voice';

export function ModelsPage() {
  const [params, setParams] = useSearchParams();
  const selected = params.get('tab');
  const tab: ModelTab = selected === 'retrieval' || selected === 'voice' ? selected : 'chat';

  return (
    <div className="tset tset-page">
      <PageHead title="模型" />

      <div className="seg set-tabs" role="tablist">
        {(
          [
            ['chat', '对话模型'],
            ['retrieval', '检索模型'],
            ['voice', '语音模型'],
          ] as Array<[ModelTab, string]>
        ).map(([key, label]) => (
          <button
            type="button"
            key={key}
            role="tab"
            aria-selected={tab === key}
            className={`seg__btn${tab === key ? ' seg__btn--on' : ''}`}
            onClick={() => setParams({ tab: key }, { replace: true })}
          >
            {label}
          </button>
        ))}
      </div>

      {tab === 'chat' && <ChatModelsSection />}
      {tab === 'retrieval' && <RetrievalModelsSection />}
      {tab === 'voice' && <VoiceModelsSection />}
    </div>
  );
}

/* ================================================================
 *  一、对话模型
 * ================================================================ */

function ChatModelsSection() {
  const [state, setState] = useState<LlmModelsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);

  /* 草稿态：与生效值不同的字段才记；保存前一律不落盘 */
  const [baseUrlDraft, setBaseUrlDraft] = useState<string | null>(null);
  const [keyDraft, setKeyDraft] = useState('');
  const [showKey, setShowKey] = useState(false);
  /** 聚焦时输入框让位给新 Key 输入；失焦才回到掩码展示 */
  const [keyFocus, setKeyFocus] = useState(false);
  const [modelDraft, setModelDraft] = useState<string | null>(null);
  /**
   * 「这家连接保存了哪些模型」的草稿（null = 没改过）。
   * 带 `profileId`：切换供应商时旧草稿不能落到新供应商名下。
   */
  const [catalogDraft, setCatalogDraft] = useState<{ profileId: string; models: string[] } | null>(
    null,
  );
  /** 新模型名输入框（「添加模型」） */
  const [newModel, setNewModel] = useState('');

  /** 连通性测试（草稿参数，POST /llm/test）：记录测试时表单的指纹，表单再变就得重测 */
  const [testing, setTesting] = useState(false);
  const [tested, setTested] = useState<{ fp: string; result: LlmProbeResult } | null>(null);
  const [testError, setTestError] = useState<string | null>(null);

  /** 供应商真实模型清单（POST /llm/models/list，草稿参数、不落盘）——「默认模型」下拉的数据源 */
  const [remoteModels, setRemoteModels] = useState<string[]>([]);
  const [remoteLoading, setRemoteLoading] = useState(false);
  const [remoteError, setRemoteError] = useState<string | null>(null);
  const [remoteFp, setRemoteFp] = useState<string | null>(null);
  /** 默认模型下拉：开合 / 搜索词 */
  const [modelPickOpen, setModelPickOpen] = useState(false);
  const [modelSearch, setModelSearch] = useState('');
  /** 下拉的 fixed 坐标（卡片外壳 overflow:hidden，absolute 会被裁掉） */
  const [pickPos, setPickPos] = useState<{
    left: number;
    width: number;
    top?: number;
    bottom?: number;
    maxHeight: number;
  } | null>(null);
  const modelPickRef = useRef<HTMLDivElement | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const next = await fetchLlmModel();
      setState(next);
      setBaseUrlDraft(null);
      setModelDraft(null);
      setCatalogDraft(null);
      setKeyDraft('');
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  /* ---- 供应商模型清单：接入地址或 Key 变了就自动重拉（草稿参数，不落盘，纯查询） ---- */
  const remoteTarget = JSON.stringify([
    (baseUrlDraft ?? state?.base_url ?? '').trim(),
    keyDraft.trim(),
  ]);
  useEffect(() => {
    if (remoteFp === remoteTarget) return;
    let cancelled = false;
    (async () => {
      setRemoteLoading(true);
      setRemoteError(null);
      try {
        const payload: { LLM_BASE_URL?: string; LLM_API_KEY?: string } = {};
        const base = (baseUrlDraft ?? state?.base_url ?? '').trim();
        const key = keyDraft.trim();
        if (base) payload.LLM_BASE_URL = base;
        if (key) payload.LLM_API_KEY = key;
        const r = await fetchLlmRemoteModels(payload);
        if (cancelled) return;
        setRemoteModels(r.ok ? r.models : []);
        if (!r.ok) setRemoteError(r.error ?? '没有拿到模型清单');
      } catch (e) {
        if (cancelled) return;
        setRemoteModels([]);
        setRemoteError(e instanceof Error ? e.message : String(e));
      } finally {
        if (!cancelled) {
          setRemoteFp(remoteTarget);
          setRemoteLoading(false);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [remoteTarget, remoteFp]);

  /* 点别处收起模型下拉（与聊天页的浮层同一手感，不搞两套） */
  useEffect(() => {
    if (!modelPickOpen) return;
    const onDown = (e: MouseEvent) => {
      if (!modelPickRef.current?.contains(e.target as Node)) setModelPickOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [modelPickOpen]);

  /* 展开期间跟随锚点：弹窗容器会滚动，fixed 坐标必须自己重算 */
  useEffect(() => {
    if (!modelPickOpen) {
      setPickPos(null);
      return;
    }
    const update = () => {
      const el = modelPickRef.current;
      if (!el) return;
      const r = el.getBoundingClientRect();
      const below = window.innerHeight - r.bottom - 16;
      const above = r.top - 16;
      const openUp = below < 220 && above > below;
      setPickPos({
        left: r.left,
        width: r.width,
        maxHeight: Math.max(160, Math.min(320, openUp ? above : below)),
        ...(openUp
          ? { bottom: window.innerHeight - r.top + 6 }
          : { top: r.bottom + 6 }),
      });
    };
    update();
    window.addEventListener('resize', update);
    const scroller = modelPickRef.current?.closest('.dmodal__scroll');
    scroller?.addEventListener('scroll', update, { passive: true });
    return () => {
      window.removeEventListener('resize', update);
      scroller?.removeEventListener('scroll', update);
    };
  }, [modelPickOpen]);

  if (loading || !state) {
    return (
      <Section title="对话模型">
        <div className="alert alert--info">{error ?? '读取中…'}</div>
      </Section>
    );
  }

  const profiles = state.profiles ?? [];
  const baseUrl = baseUrlDraft ?? state.base_url;
  /** 表单当前 Base URL 属于目录里哪个 profile —— chip 高亮与 Key 行的归属都跟它走 */
  const selectedProfile = profiles.find((p) => p.base_url === baseUrl);
  const keyMask = selectedProfile?.api_key_masked ?? '';
  const model = modelDraft ?? state.model;
  const candBase = baseUrl.trim();
  const candModel = model.trim();

  /**
   * 「已保存」= 这家连接保存成功的模型（后端 `profiles[].models`）。
   * 草稿只属于当前选中的供应商；切到别家时旧草稿不能跟过去。
   */
  const savedModels = selectedProfile?.models ?? [];
  const saved =
    catalogDraft && catalogDraft.profileId === selectedProfile?.id
      ? catalogDraft.models
      : savedModels;
  const catalogDirty =
    selectedProfile !== undefined &&
    catalogDraft !== null &&
    catalogDraft.profileId === selectedProfile.id &&
    catalogDraft.models.join('\u0000') !== savedModels.join('\u0000');

  const baseChanged = candBase !== state.base_url.trim();
  const modelChanged = candModel !== state.model.trim();
  const candKey = keyDraft.trim();
  const connDirty = baseChanged || modelChanged || candKey !== '';
  const dirty = connDirty || catalogDirty;

  /**
   * 只有「这组参数没在当前连接里保存过」才必须测试：
   * - 换了 Base URL 到一家没有 Key 的供应商；
   * - 手动填了新 Key；
   * - 选了这家还没保存过的新模型。
   *
   * 在两家**已保存**的连接之间切换不该再打一次模型 —— 那既浪费配额，
   * 也只是重复证明一件已经证明过的事。
   */
  const profileForCand = profiles.find((p) => p.base_url === candBase) ?? null;
  const needsTest =
    candKey !== '' ||
    (baseChanged && !profileForCand?.api_key_present) ||
    (modelChanged && !profileForCand?.models.includes(candModel));
  /** 指纹：这组参数测过之后表单又变没变，比对它就知道要不要重测
   * （Key 一项用「草稿 || 该供应商目录掩码」：切供应商时有效 Key 变了，旧测试作废） */
  const fingerprint = JSON.stringify([candBase, candModel, candKey || keyMask]);
  const freshTest = tested && tested.fp === fingerprint ? tested.result : null;
  const testedOk = freshTest?.ok === true;
  const canSave = dirty && (!needsTest || testedOk);
  const providerLabel = profileForCand?.name ?? '自定义接入';

  /**
   * 「默认模型」下拉按供应商分组。
   *
   * - 当前表单这家：供应商实时清单（`/v1/models`）+ 已保存模型；
   *   实时清单拉不到时退回预设建议；
   * - 其他家：只列已保存模型（没有 Key 的供应商不拉、也拉不到）；
   * - 点别家的模型会**同时**把表单切到那家连接 —— 模型与地址不能错配。
   */
  const pickGroups: Array<{
    key: string;
    label: string;
    profile: LlmProfileView | null;
    models: string[];
  }> = [];
  const pushPickGroup = (
    key: string,
    label: string,
    profile: LlmProfileView | null,
    names: string[],
  ) => {
    const q = modelSearch.trim().toLowerCase();
    const models = Array.from(new Set(names.map((n) => n.trim()).filter(Boolean))).filter(
      (n) => !q || n.toLowerCase().includes(q),
    );
    if (models.length) pickGroups.push({ key, label, profile, models });
  };
  if (selectedProfile) {
    const live = remoteError ? (selectedProfile.preset_models ?? []) : remoteModels;
    pushPickGroup(selectedProfile.id, selectedProfile.name, selectedProfile, [
      ...live,
      ...saved,
      candModel,
    ]);
  } else if (candBase) {
    pushPickGroup('__current__', '当前接入', null, [...remoteModels, candModel]);
  }
  for (const p of profiles) {
    if (p.id === selectedProfile?.id) continue;
    pushPickGroup(p.id, p.name, p, p.models);
  }

  /** 手动重拉供应商清单：把指纹归零，让上面那个自动拉取的 effect 再跑一遍 */
  const refreshRemote = () => setRemoteFp(null);

  /** 选供应商 = 预填表单（Base URL + 该家保存过的第一个模型）；保存才落盘 */
  const pickProvider = (p: LlmProfileView) => {
    if (p.base_url === baseUrl) return; // 已经是这家：别把「有改动」点亮
    setBaseUrlDraft(p.base_url);
    setKeyDraft('');
    setCatalogDraft(null);
    // 预填这家保存过的模型，避免「换了供应商、模型名还是上一家的」
    if (p.models[0]) setModelDraft(p.models[0]);
  };

  /** 从「默认模型」下拉里选一条：跨供应商时连连接一起切过去 */
  const pickModelOption = (profile: LlmProfileView | null, name: string) => {
    if (profile && profile.base_url !== baseUrl) {
      setBaseUrlDraft(profile.base_url);
      setKeyDraft('');
      setCatalogDraft(null);
    }
    setModelDraft(name);
    setModelPickOpen(false);
  };

  /** 点已保存的 chip = 选中那家连接 + 那个模型（对话页下拉同一语义） */
  const pickSaved = (p: LlmProfileView, name: string) => {
    pickModelOption(p, name);
  };

  /** 「已保存的模型」按连接分组；没配 Key 的连接不列（列了也用不了） */
  const savedGroups = profiles
    .filter((p) => p.api_key_present)
    .map((p) => ({
      profile: p,
      models: catalogDraft?.profileId === p.id ? catalogDraft.models : p.models,
    }))
    .filter((g) => g.models.length > 0);

  const save = async () => {
    setError(null);
    setWarnings([]);
    if (!candBase) {
      setError('Base URL 不能为空');
      return;
    }
    if (!candModel) {
      setError('默认模型不能为空');
      return;
    }
    try {
      const patch: Record<string, string> = {};
      if (baseChanged) patch.LLM_BASE_URL = candBase;
      if (modelChanged) patch.LLM_MODEL = candModel;
      if (candKey) patch.LLM_API_KEY = candKey;
      // 只提交**当前选中这家**的已保存清单；没有 profile 的新地址先保存连接，
      // 后端会把这次选的模型记到新建的 profile 名下。
      if (selectedProfile && catalogDirty && catalogDraft) {
        patch.LLM_AVAILABLE_MODELS = catalogDraft.models.join(', ');
      }
      // 表单属于目录里哪个 profile；手填的新 Base URL 由后端按 URL 建 custom
      if (selectedProfile) patch.profile_id = selectedProfile.id;
      const next = await updateLlmModel(patch);
      setState(next);
      setBaseUrlDraft(null);
      setModelDraft(null);
      setCatalogDraft(null);
      setKeyDraft('');
      setWarnings(next.warnings ?? []);
      // 保存后草稿清空 —— 若刚才通过测试的就是这份配置，把绿灯续到已保存的指纹上
      const savedMask =
        (next.profiles ?? []).find((p) => p.base_url === next.base_url)?.api_key_masked ?? '';
      setTested(
        testedOk
          ? { fp: JSON.stringify([next.base_url, next.model, savedMask]), result: tested!.result }
          : null,
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  /** 用表单草稿参数测连通性（不落盘）；没有改动时等价于自检当前配置 */
  const testNow = async () => {
    setTesting(true);
    setTestError(null);
    if (!candBase) {
      setTestError('Base URL 不能为空');
      setTesting(false);
      return;
    }
    if (!candModel) {
      setTestError('默认模型不能为空');
      setTesting(false);
      return;
    }
    try {
      const payload: Record<string, string> = {};
      if (candBase) payload.LLM_BASE_URL = candBase;
      if (candModel) payload.LLM_MODEL = candModel;
      if (candKey) payload.LLM_API_KEY = candKey;
      const result = await testLlmModel(payload);
      setTested({ fp: fingerprint, result });
    } catch (e) {
      setTestError(e instanceof Error ? e.message : String(e));
    } finally {
      setTesting(false);
    }
  };

  const resetAll = async () => {
    setError(null);
    setWarnings([]);
    try {
      const next = await resetLlmModel();
      setState(next);
      setBaseUrlDraft(null);
      setModelDraft(null);
      setCatalogDraft(null);
      setKeyDraft('');
      setTested(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  /** 已保存清单的草稿操作（保存才落盘） */
  const addModel = (name: string) => {
    const clean = name.trim();
    if (!clean) return;
    if (!selectedProfile) {
      setError('先把这条连接保存下来，再维护它的模型清单');
      return;
    }
    setCatalogDraft({
      profileId: selectedProfile.id,
      models: Array.from(new Set([...saved, clean])),
    });
    setNewModel('');
  };

  const removeModel = (name: string) => {
    if (!selectedProfile) return;
    // 当前生效连接的默认模型不能从清单里删（它正在被使用）
    if (name === state.model && selectedProfile.base_url === state.base_url) return;
    setCatalogDraft({
      profileId: selectedProfile.id,
      models: saved.filter((n) => n !== name),
    });
  };

  /* 连接状态卡文案 —— 永远在最上面回答「这条链路现在通不通」 */
  let tone: 'ok' | 'warn' | 'bad' | 'idle' = 'idle';
  let statusTitle = '';
  let statusSub = '';
  if (needsTest) {
    if (testedOk) {
      tone = 'ok';
      statusTitle = '连接测试通过';
      statusSub = `${candModel} @ ${candBase} · ${formatMs(freshTest!.ms)}`;
    } else if (freshTest) {
      tone = 'bad';
      statusTitle = '连接测试未通过';
      statusSub = freshTest.error ?? '没有拿到回复';
    } else {
      tone = 'warn';
      statusTitle = '改动待验证';
      statusSub = `${candModel} @ ${candBase}`;
    }
  } else if (connDirty) {
    tone = 'ok';
    statusTitle = '使用已保存的连接';
    statusSub = `${candModel} @ ${providerLabel}`;
  } else if (catalogDirty) {
    tone = 'idle';
    statusTitle = '已保存的模型有改动';
    statusSub = `${candModel} @ ${providerLabel}`;
  } else if (freshTest) {
    tone = freshTest.ok ? 'ok' : 'bad';
    statusTitle = freshTest.ok ? '连接正常' : '连接失败';
    statusSub = freshTest.ok
      ? `${state.model} @ ${state.base_url} · ${formatMs(freshTest.ms)}`
      : (freshTest.error ?? '没有拿到回复');
  } else if (state.api_key_present) {
    tone = 'idle';
    statusTitle = '已配置（尚未验证连通）';
    statusSub = `${state.model} · Key ${state.api_key_masked ?? ''}`;
  } else {
    tone = 'warn';
    statusTitle = '尚未配置';
  }

  return (
    <Section title="对话模型">
      {/* ---------- 连接状态 ---------- */}
      <div className={`conn conn--${tone}`}>
        <span className="conn__dot" aria-hidden />
        <div className="conn__body">
          <div className="conn__title">{statusTitle}</div>
          {statusSub && <div className="conn__sub">{statusSub}</div>}
        </div>
        <span className="badge badge--muted conn__badge">{providerLabel}</span>
      </div>

      {/* ---------- 供应商目录（后端持有，各家各自的 Key） ---------- */}
      <div className="prov-chips">
        {profiles.map((p) => (
          <button
            type="button"
            key={p.id}
            className={`prov-chip${selectedProfile?.id === p.id ? ' prov-chip--on' : ''}`}
            onClick={() => pickProvider(p)}
            title={`${p.base_url} · ${p.api_key_present ? `Key 已配置 ${p.api_key_masked}` : 'Key 未配置'}`}
          >
            {p.name}
          </button>
        ))}
        {/* 目录里的手填端点已经占着「自定义端点」这个名字，再常驻一个同名入口就是重复：
            只在表单里的地址落不到任何目录项时，才给一个「自己填」的落点 */}
        {!selectedProfile && (
          <button
            type="button"
            className="prov-chip prov-chip--on"
            onClick={() => setBaseUrlDraft('')}
            title="手动填写接口地址"
          >
            自定义
          </button>
        )}
      </div>

      {/* ---------- 接入表单 ---------- */}
      <Rows>
        <Row
          label="Base URL"
          control={
            <input
              className="input ctl-lg"
              value={baseUrl}
              spellCheck={false}
              onChange={(e) => setBaseUrlDraft(e.target.value)}
              placeholder="https://api.example.com/v1"
            />
          }
        />
        <Row label="API 格式" control={<span className="badge badge--muted">OpenAI 兼容</span>} />
        <Row
          label="API Key"
          control={
            <span className="set-inline">
              <input
                className="input ctl-md"
                /* 掩码本身不含密钥，没必要再套一层 password —— 配没配、尾号是哪四位，
                   就看这个框：有值就直说，空着就是没配。只有真正在打的草稿才需要遮起来。 */
                type={keyDraft && !showKey ? 'password' : 'text'}
                value={keyFocus ? keyDraft : keyDraft || keyMask}
                onChange={(e) => setKeyDraft(e.target.value)}
                onFocus={() => setKeyFocus(true)}
                onBlur={() => setKeyFocus(false)}
                placeholder={selectedProfile?.api_key_present ? '留空 = 不修改' : '输入 API Key'}
                spellCheck={false}
                autoComplete="off"
              />
              <button
                type="button"
                className="icon-btn"
                onClick={() => setShowKey((v) => !v)}
                disabled={!keyDraft}
                title={showKey ? '隐藏' : '显示'}
              >
                {showKey ? <EyeOff size={13} /> : <Eye size={13} />}
              </button>
              {selectedProfile?.key_url && (
                <a
                  className="icon-btn"
                  href={selectedProfile.key_url}
                  target="_blank"
                  rel="noreferrer noopener"
                  title="去官网获取 Key"
                >
                  <ExternalLink size={13} />
                </a>
              )}
            </span>
          }
        />
        <Row
          label="默认模型"
          control={
            <div ref={modelPickRef} className="set-pick">
              <span className="set-pick__row">
                <input
                  className="input set-pick__input"
                  value={model}
                  spellCheck={false}
                  onChange={(e) => {
                    setModelDraft(e.target.value);
                    setModelSearch(e.target.value);
                  }}
                  onFocus={() => setModelPickOpen(true)}
                  placeholder={state.default || '模型名'}
                />
                <button
                  type="button"
                  className="btn btn--sm btn--ghost"
                  onClick={() => setModelPickOpen((v) => !v)}
                  title="从供应商的模型清单里选"
                >
                  <ChevronDown size={13} />
                </button>
                <button
                  type="button"
                  className="btn btn--sm btn--ghost"
                  onClick={refreshRemote}
                  disabled={remoteLoading}
                  title="重新拉取模型清单"
                >
                  {remoteLoading ? <span className="spinner" /> : <RefreshCw size={13} />}
                </button>
              </span>
              {modelPickOpen && (
                <div
                  className="set-pick__pop"
                  style={
                    pickPos
                      ? {
                          left: pickPos.left,
                          width: pickPos.width,
                          maxHeight: pickPos.maxHeight,
                          top: pickPos.top,
                          bottom: pickPos.bottom,
                        }
                      : { visibility: 'hidden' }
                  }
                >
                  <div className="set-pick__search">
                    <input
                      className="input"
                      value={modelSearch}
                      onChange={(e) => setModelSearch(e.target.value)}
                      placeholder="搜索模型…"
                      spellCheck={false}
                      autoFocus
                    />
                  </div>
                  <div className="set-pick__list">
                    {pickGroups.length === 0 && (
                      <div className="set-pick__empty">
                        {remoteLoading ? '正在拉取模型清单…' : (remoteError ?? '没有可选的模型')}
                      </div>
                    )}
                    {pickGroups.map((group) => (
                      <div key={group.key} className="set-pick__group">
                        <div className="set-pick__group-label">{group.label}</div>
                        {group.models.map((name) => {
                          const picked = name === candModel && group.profile
                            ? group.profile.base_url === baseUrl
                            : name === candModel;
                          return (
                            <button
                              type="button"
                              key={`${group.key}:${name}`}
                              className={`set-pick__opt${picked ? ' set-pick__opt--on' : ''}`}
                              onClick={() => pickModelOption(group.profile, name)}
                            >
                              <span>{name}</span>
                              {picked && <Check size={13} />}
                            </button>
                          );
                        })}
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          }
        />
      </Rows>

      {/* ---------- 已保存的模型（按连接归属；对话页下拉的唯一来源） ---------- */}
      <Rows>
        <Row
          stack
          label="已保存的模型"
          title="保存后出现在对话页下拉"
          control={
            <div className="set-model-chips">
              {savedGroups.length === 0 && (
                <span className="set-model-chips__empty">还没有保存成功的模型</span>
              )}
              {savedGroups.map(({ profile: p, models: names }) => (
                <div key={p.id} className="set-model-chips__group">
                  <span className="set-model-chips__label">{p.name}</span>
                  {names.map((name) => {
                    const isCurrent = p.id === selectedProfile?.id && name === candModel;
                    // 当前生效连接的默认模型不能从清单里删（它正在被使用）
                    const removable =
                      p.id === selectedProfile?.id &&
                      !(name === state.model && p.base_url === state.base_url);
                    return (
                      <span key={name} className={`rag-chip${isCurrent ? ' rag-chip--on' : ''}`}>
                        <button
                          type="button"
                          className="set-model-chips__pick"
                          onClick={() => pickSaved(p, name)}
                          title={
                            isCurrent
                              ? `${name}（当前默认）`
                              : `切到 ${p.name} 的 ${name}（连接一起切换）`
                          }
                        >
                          {name}
                        </button>
                        {removable && (
                          <button
                            type="button"
                            className="set-model-chips__remove"
                            onClick={() => removeModel(name)}
                            title={`从 ${p.name} 的清单移除 ${name}`}
                          >
                            <X size={11} />
                          </button>
                        )}
                      </span>
                    );
                  })}
                </div>
              ))}
              {selectedProfile && (
                <span className="set-model-chips__add">
                  <input
                    className="input ctl-sm"
                    value={newModel}
                    onChange={(e) => setNewModel(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') {
                        e.preventDefault();
                        addModel(newModel);
                      }
                    }}
                    placeholder={`添加到「${selectedProfile.name}」…`}
                    spellCheck={false}
                  />
                  <button
                    type="button"
                    className="btn btn--sm"
                    onClick={() => addModel(newModel)}
                    disabled={!newModel.trim()}
                    title="保存该模型"
                  >
                    <Plus size={12} />
                  </button>
                </span>
              )}
            </div>
          }
        />
      </Rows>

      {/* ---------- 动作条：测试连接是保存的前置闸门 ---------- */}
      <div className="set-toolbar">
        <span className="set-toolbar__meta">{dirty ? '有未保存的改动' : ''}</span>
        <button
          type="button"
          className={`btn btn--sm${connDirty && !testedOk ? ' btn--primary' : ''}`}
          onClick={() => void testNow()}
          disabled={testing}
          title="试连一次，不保存配置"
        >
          {testing ? <span className="spinner" /> : <Stethoscope size={13} />} 测试连接
        </button>
        <button
          type="button"
          className="btn btn--sm btn--primary"
          onClick={() => void save()}
          disabled={!canSave}
          title={
            !dirty
              ? '没有要保存的改动'
              : needsTest && !testedOk
                ? '先「测试连接」，通过后才能保存'
                : undefined
          }
        >
          <Zap size={13} /> 保存
        </button>
        <button
          type="button"
          className="btn btn--sm btn--ghost"
          onClick={() => void resetAll()}
          disabled={!state.overridden && !state.base_url_overridden}
        >
          恢复默认
        </button>
        <button type="button" className="btn btn--sm btn--ghost" onClick={() => void load()}>
          <RefreshCw size={13} /> 刷新
        </button>
      </div>

      {error && <div className="alert alert--error">{error}</div>}
      {warnings.length > 0 && (
        <div className="alert alert--warn">
          已保存，但请注意：{warnings.join('；')}
        </div>
      )}
      {testError && <div className="alert alert--error">{testError}</div>}
      {freshTest && (
        <Rows>
          <Row
            label={`${freshTest.ok ? '测试通过' : '测试失败'} · ${freshTest.model}`}
            desc={
              freshTest.ok
                ? `回复「${freshTest.reply ?? ''}」`
                : (freshTest.error ?? '没有拿到回复')
            }
            control={
              <span className={`badge badge--${freshTest.ok ? 'safe' : 'danger'}`}>
                {freshTest.ok ? formatMs(freshTest.ms) : '失败'}
              </span>
            }
          />
        </Rows>
      )}
    </Section>
  );
}

/* ================================================================
 *  二、检索模型（自填 api 供应商 或 本地模型 —— 不提供预设）
 * ================================================================ */

function RetrievalModelsSection() {
  const [state, setState] = useState<RagModelsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [probing, setProbing] = useState(false);
  const [probe, setProbe] = useState<RagProbeResult | null>(null);
  /** 草稿：只记与生效值不同的字段 */
  const [draft, setDraft] = useState<Record<string, string> | null>(null);

  const load = useCallback(async () => {
    try {
      setState(await fetchRagModels());
      setDraft(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (!state) {
    return (
      <Section title="检索模型">
        <div className="alert alert--info">{error ?? '读取中…'}</div>
      </Section>
    );
  }

  const cfg = state.config as Record<string, string>;
  const value = (key: string): string => draft?.[key] ?? String(cfg[key] ?? '');
  const dirty = draft !== null && Object.keys(draft).length > 0;
  const embeddingDirty =
    draft !== null && Object.keys(draft).some((k) => k.startsWith('EMBEDDING_'));

  const setField = (key: string, val: string) => {
    setDraft((d) => {
      const next = { ...d, [key]: val };
      // 草稿值与生效值一致时移除该键（「没改过」的语义）
      if (next[key] === String(cfg[key] ?? '')) delete next[key];
      return Object.keys(next).length ? next : null;
    });
  };

  const save = async () => {
    setError(null);
    setWarnings([]);
    try {
      const next = await updateRagModels(draft ?? {});
      setState(next);
      setDraft(null);
      setProbe(null);
      setWarnings(next.warnings ?? []);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const probeNow = async () => {
    setProbing(true);
    try {
      setProbe(await probeRagModels());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setProbing(false);
    }
  };

  const embBackend = value('EMBEDDING_BACKEND');
  const rrkBackend = value('RERANK_BACKEND');

  const keyField = (key: string, label: string): React.ReactNode => (
    <input
      className="input ctl-md"
      type="password"
      value={value(key)}
      onChange={(e) => setField(key, e.target.value)}
      placeholder={label}
      spellCheck={false}
      autoComplete="off"
    />
  );

  return (
    <Section title="检索模型">
      {embeddingDirty && (
        <div className="alert alert--warn">更换 embedding 会导致已入库向量作废，需要重建向量库</div>
      )}
      {/* ---------- Embedding ---------- */}
      <Rows>
        <Row
          label="Embedding · 后端"
          control={
            <select
              className="select ctl-sm"
              value={embBackend}
              onChange={(e) => setField('EMBEDDING_BACKEND', e.target.value)}
            >
              <option value="api">API 供应商</option>
              <option value="local">本地模型</option>
            </select>
          }
        />
        {embBackend === 'api' ? (
          <>
            <Row
              label="Embedding · Base URL"
              control={
                <input
                  className="input ctl-lg"
                  value={value('EMBEDDING_BASE_URL')}
                  onChange={(e) => setField('EMBEDDING_BASE_URL', e.target.value)}
                  placeholder="https://api.siliconflow.cn/v1"
                  spellCheck={false}
                />
              }
            />
            <Row label="Embedding · API Key" control={keyField('EMBEDDING_API_KEY', '留空 = 沿用现有值')} />
            <Row
              label="Embedding · 模型"
              control={
                <input
                  className="input ctl-md"
                  value={value('EMBEDDING_MODEL')}
                  onChange={(e) => setField('EMBEDDING_MODEL', e.target.value)}
                  placeholder="BAAI/bge-m3"
                  spellCheck={false}
                />
              }
            />
          </>
        ) : (
          <Row
            label="Embedding · 本地模型"
            control={
              <input
                className="input ctl-lg"
                value={value('EMBEDDING_LOCAL_MODEL')}
                onChange={(e) => setField('EMBEDDING_LOCAL_MODEL', e.target.value)}
                placeholder="本机模型目录或 HF id（如 BAAI/bge-small-zh-v1.5）"
                spellCheck={false}
              />
            }
          />
        )}
      </Rows>

      {/* ---------- Rerank ---------- */}
      <Rows>
        <Row
          label="Rerank · 后端"
          control={
            <select
              className="select ctl-sm"
              value={rrkBackend}
              onChange={(e) => setField('RERANK_BACKEND', e.target.value)}
            >
              <option value="api">API 供应商</option>
              <option value="local">本地模型</option>
              <option value="none">关闭</option>
            </select>
          }
        />
        {rrkBackend === 'api' && (
          <>
            <Row
              label="Rerank · API URL"
              control={
                <input
                  className="input ctl-lg"
                  value={value('RERANK_API_URL')}
                  onChange={(e) => setField('RERANK_API_URL', e.target.value)}
                  placeholder="https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
                  title="留空 = 从对话模型 Base URL 推导"
                  spellCheck={false}
                />
              }
            />
            <Row label="Rerank · API Key" control={keyField('RERANK_API_KEY', '留空 = 复用对话模型的 API Key')} />
          </>
        )}
        {rrkBackend !== 'none' && (
          <Row
            label={rrkBackend === 'api' ? 'Rerank · 模型' : 'Rerank · 本地模型'}
            control={
              <input
                className="input ctl-md"
                value={value(rrkBackend === 'api' ? 'RERANK_MODEL' : 'RERANK_LOCAL_MODEL')}
                onChange={(e) =>
                  setField(rrkBackend === 'api' ? 'RERANK_MODEL' : 'RERANK_LOCAL_MODEL', e.target.value)
                }
                placeholder={rrkBackend === 'api' ? '供应商的 rerank 模型名' : '本机模型目录或 HF id'}
                spellCheck={false}
              />
            }
          />
        )}
      </Rows>

      <div className="set-toolbar">
        <span className="set-toolbar__meta">{dirty ? '有未保存的改动' : ''}</span>
        <button type="button" className="btn btn--sm btn--primary" onClick={() => void save()} disabled={!dirty}>
          <Zap size={13} /> 保存
        </button>
        <button type="button" className="btn btn--sm" onClick={() => void probeNow()} disabled={probing}>
          {probing ? <span className="spinner" /> : <Stethoscope size={13} />} 探测
        </button>
      </div>

      {error && <div className="alert alert--error">{error}</div>}
      {warnings.length > 0 && (
        <div className="alert alert--warn">
          已保存，但请注意：{warnings.join('；')}
        </div>
      )}
      {probe && (
        <Rows>
          <Row
            label="Embedding 探测"
            desc={
              probe.embedding.ok
                ? `${probe.embedding.model} · ${probe.embedding.dimension ?? '?'} 维`
                : (probe.embedding.error ?? '失败')
            }
            control={
              <span className={`badge badge--${probe.embedding.ok ? 'safe' : 'danger'}`}>
                {probe.embedding.ok ? formatMs(probe.embedding.ms) : '失败'}
              </span>
            }
          />
          <Row
            label="Rerank 探测"
            desc={
              probe.rerank.ok
                ? `分数 [${(probe.rerank.scores ?? []).map((s) => (s === null ? '—' : s.toFixed(3))).join(', ')}]`
                : (probe.rerank.error ?? '失败')
            }
            control={
              <span className={`badge badge--${probe.rerank.ok ? 'safe' : 'danger'}`}>
                {probe.rerank.ok ? formatMs(probe.rerank.ms) : '失败'}
              </span>
            }
          />
        </Rows>
      )}
    </Section>
  );
}

/* ================================================================
 *  三、语音模型（STT / TTS；Key 目前跟随生效对话连接）
 * ================================================================ */

function VoiceModelsSection() {
  const [state, setState] = useState<VoiceSettingsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [probing, setProbing] = useState(false);
  const [probe, setProbe] = useState<VoiceProbeResult | null>(null);
  const [draft, setDraft] = useState<Record<string, string> | null>(null);

  const load = useCallback(async () => {
    try {
      setState(await fetchVoiceSettings());
      setDraft(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (!state) {
    return (
      <Section title="语音模型">
        <div className="alert alert--info">{error ?? '读取中…'}</div>
      </Section>
    );
  }

  const cfg = state.config;
  const value = (key: keyof VoiceSettingsResponse['config']): string => draft?.[key] ?? cfg[key];
  const dirty = draft !== null && Object.keys(draft).length > 0;
  const sttBackend = value('VOICE_STT_BACKEND');
  const ttsBackend = value('VOICE_TTS_BACKEND');

  const setField = (key: keyof VoiceSettingsResponse['config'], val: string) => {
    setDraft((d) => {
      const next = { ...(d ?? {}), [key]: val };
      if (next[key] === cfg[key]) delete next[key];
      return Object.keys(next).length ? next : null;
    });
  };

  const save = async () => {
    setError(null);
    setWarnings([]);
    try {
      const next = await updateVoiceSettings(draft ?? {});
      setState(next);
      setDraft(null);
      setProbe(null);
      setWarnings(next.warnings ?? []);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const resetAll = async () => {
    setError(null);
    try {
      const next = await resetVoiceSettings();
      setState(next);
      setDraft(null);
      setProbe(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const probeNow = async () => {
    setProbing(true);
    try {
      setProbe(await probeVoiceSettings());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setProbing(false);
    }
  };

  const overridden = Object.keys(state.store?.active ?? {}).some((k) => k.startsWith('VOICE_'));

  return (
    <Section title="语音模型" desc="提问识别负责把你的话转成文字；回答声音负责让 Syncore 开口。桌面助手的唤醒回应、回答和提醒共用这里的朗读配置，唤醒词检测模型在桌面助手设置中单独管理。">
      {!state.api_key_set && (sttBackend === 'dashscope' || ttsBackend === 'dashscope') && (
        <div className="alert alert--warn">
          未配置 API Key —— 云端识别 / DashScope 朗读不可用；本地识别与 Edge 朗读不受影响。
        </div>
      )}

      <Rows>
        <Row
          label="语音识别（STT）· 引擎"
          control={
            <select
              className="select ctl-sm"
              value={sttBackend}
              onChange={(e) => setField('VOICE_STT_BACKEND', e.target.value)}
            >
              <option value="local">本地（SenseVoice）</option>
              <option value="dashscope">DashScope（云端）</option>
            </select>
          }
        />
        {sttBackend === 'local' ? (
          <Row
            label="语音识别（STT）· 本地模型"
            desc={state.local_stt?.available ? undefined : state.local_stt?.reason ?? '模型未就绪'}
            title={state.local_stt?.path ? `模型目录：${state.local_stt.path}` : undefined}
            control={
              <span className={`badge badge--${state.local_stt?.available ? 'safe' : 'danger'}`}>
                {state.local_stt?.available ? '已就绪' : '未安装'}
              </span>
            }
          />
        ) : (
          <Row
            label="语音识别（STT）· 模型"
            control={
              <input
                className="input ctl-md"
                value={value('VOICE_STT_MODEL')}
                onChange={(e) => setField('VOICE_STT_MODEL', e.target.value)}
                placeholder="qwen3-asr-flash"
                spellCheck={false}
              />
            }
          />
        )}
        <Row
          label="朗读（TTS）· 后端"
          desc="用于桌面助手语音回复、唤醒回应和日常播报"
          control={
            <select
              className="select ctl-sm"
              value={ttsBackend}
              onChange={(e) => setField('VOICE_TTS_BACKEND', e.target.value)}
            >
              <option value="dashscope">DashScope</option>
              <option value="edge">Edge（免费，需联网）</option>
            </select>
          }
        />
        {ttsBackend === 'dashscope' && <Row
          label="朗读（TTS）· 模型"
          desc="当前模型支持边生成音频边播放，无需等待整段音频下载"
          control={
            <input
              className="input ctl-md"
              value={value('VOICE_TTS_MODEL')}
              onChange={(e) => setField('VOICE_TTS_MODEL', e.target.value)}
              placeholder="qwen3-tts-flash"
              spellCheck={false}
            />
          }
        />}
        {ttsBackend === 'dashscope' ? (
          <Row
            label="朗读（TTS）· 音色"
            control={
              <input
                className="input ctl-md"
                value={value('VOICE_TTS_VOICE')}
                onChange={(e) => setField('VOICE_TTS_VOICE', e.target.value)}
                placeholder="Cherry"
                title="DashScope 音色名"
                spellCheck={false}
              />
            }
          />
        ) : (
          <>
            <Row
              label="朗读（TTS）· 音色"
              control={
                <input
                  className="input ctl-md"
                  value={value('VOICE_TTS_VOICE_EDGE')}
                  onChange={(e) => setField('VOICE_TTS_VOICE_EDGE', e.target.value)}
                  placeholder="微软 Edge 音色名（zh-CN-XiaoxiaoNeural 等）"
                  spellCheck={false}
                />
              }
            />
            <Row
              label="朗读（TTS）· 语速"
              control={
                <input
                  className="input ctl-sm"
                  value={value('VOICE_TTS_RATE')}
                  onChange={(e) => setField('VOICE_TTS_RATE', e.target.value)}
                  placeholder="+0%"
                  title="形如 +0% / -10% / +20%"
                  spellCheck={false}
                />
              }
            />
          </>
        )}
      </Rows>

      <div className="set-toolbar">
        <span className="set-toolbar__meta">{dirty ? '有未保存的改动' : ''}</span>
        <button type="button" className="btn btn--sm btn--primary" onClick={() => void save()} disabled={!dirty}>
          <Zap size={13} /> 保存
        </button>
        <button type="button" className="btn btn--sm" onClick={() => void probeNow()} disabled={probing} title="合成一小段语音验证配置">
          {probing ? <span className="spinner" /> : <Stethoscope size={13} />} 探测
        </button>
        <button type="button" className="btn btn--sm btn--ghost" onClick={() => void resetAll()} disabled={!overridden}>
          恢复默认
        </button>
      </div>

      {error && <div className="alert alert--error">{error}</div>}
      {warnings.length > 0 && (
        <div className="alert alert--warn">
          已保存，但请注意：{warnings.join('；')}
        </div>
      )}
      {probe && (
        <Rows>
          <Row
            label={`TTS 探测 · ${probe.model}`}
            desc={
              probe.ok
                ? `首段音频 ${formatMs(probe.first_audio_ms ?? probe.ms)} · 整段合成 ${formatMs(probe.ms)} · 音色 ${probe.voice ?? '—'}`
                : (probe.error ?? '失败')
            }
            control={
              <span className={`badge badge--${probe.ok ? 'safe' : 'danger'}`}>
                {probe.ok ? probe.streaming ? '流式播放' : '整段播放' : '失败'}
              </span>
            }
          />
        </Rows>
      )}
    </Section>
  );
}

export default ModelsPage;
