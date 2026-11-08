/**
 * 「新建工作区」弹窗（界面参照 Qoder 桌面版：源文件夹 / 名称 / 图标 / 颜色）
 *
 * ★ 选目录分两条路，按运行环境走
 *
 * 桌面壳里点「源文件夹」直接开**系统目录对话框**（`dialog:selectFolder`，
 * 见 `desktop/main.js`）—— 层级、盘符、新建文件夹都是系统那套现成手感，
 * 路径由用户在系统窗口里选定，页面插不进手。
 * Web 模式没有这座桥，退回应用内目录浏览器：后端列子目录、后端校验存在性，
 * 前端连文件名都不碰（`/workdirs/browse` 只返回目录，见 `app/api/workdirs.py`）。
 * 桌面端也留一条「手动输入路径」—— 粘一条长路径比在对话框里点八层快。
 *
 * ★ 提交之前它什么都不是
 * 点「创建」才 POST。中途关掉、Esc、换目录，后端一条记录都不会有 ——
 * 授权必须是显式的。
 */

import { useEffect, useMemo, useRef, useState } from 'react';

import {
  ArrowLeft,
  Check,
  ChevronRight,
  Folder,
  FolderOpen,
  Home,
  Loader,
  Search,
  X,
} from '@/components/icons';
import { WORKDIR_ICONS, workdirIcon } from '@/components/WorkdirPicker';
import { useT } from '@/i18n';
import { browseWorkdir, fetchWorkdirRoots } from '@/lib/api';
import { useWorkdirStore } from '@/stores/workdirStore';
import { toast } from '@/stores/toastStore';
import { isDesktopShell } from '@/types/desktop';
import type { WorkdirBrowseResult, WorkdirRoot } from '@/types';

/** 可选颜色 —— 后端只校验 `#rrggbb`，所以这里是一份纯界面清单（首个是品牌绛红） */
const COLORS = [
  '#8b1e3f',
  '#b91c1c',
  '#c2410c',
  '#b45309',
  '#a16207',
  '#15803d',
  '#0f766e',
  '#0e7490',
  '#1d4ed8',
  '#6d28d9',
  '#a21caf',
  '#475569',
];

const NAME_MAX = 40;

/** `B:\My Notebook\量化选股` → `量化选股`（名称默认取目录名，两种分隔符都要认） */
function basename(p: string): string {
  const parts = p.split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] ?? p;
}

function messageOf(e: unknown, fallback: string): string {
  return e instanceof Error && e.message ? e.message : fallback;
}

interface WorkdirCreateDialogProps {
  /** 创建成功后直接绑到这段会话 —— 用户新建它的动机就是「现在要用」 */
  threadId: string;
  onClose: () => void;
}

export function WorkdirCreateDialog({ threadId, onClose }: WorkdirCreateDialogProps) {
  const t = useT();
  const native = isDesktopShell();

  const [picked, setPicked] = useState('');
  const [browsing, setBrowsing] = useState(false);
  const [roots, setRoots] = useState<WorkdirRoot[]>([]);
  const [current, setCurrent] = useState('');
  const [browse, setBrowse] = useState<WorkdirBrowseResult | null>(null);
  const [filter, setFilter] = useState('');
  const [busyDir, setBusyDir] = useState(false);
  const [picking, setPicking] = useState(false);

  const [name, setName] = useState('');
  const [nameTouched, setNameTouched] = useState(false);
  const [icon, setIcon] = useState('folder');
  const [color, setColor] = useState(COLORS[0]);
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const create = useWorkdirStore((s) => s.create);
  const bind = useWorkdirStore((s) => s.bind);
  const declaredIcons = useWorkdirStore((s) => s.icons);

  // 图标清单以后端那份为准（`ICON_NAMES`），否则它加了名字、这里还是老一套，
  // 存进去的名字前端画不出来 —— 不报错，只是全都长成文件夹。
  // 但只有后端认识的画不出来也不行，所以取交集；清单还没拉到时用自己的顺序兜底。
  const iconChoices = useMemo(() => {
    const known = new Set(WORKDIR_ICONS.map(([key]) => key));
    const fromApi = declaredIcons.filter((key) => known.has(key));
    return fromApi.length > 0 ? fromApi : WORKDIR_ICONS.map(([key]) => key);
  }, [declaredIcons]);

  useEffect(() => {
    if (!iconChoices.includes(icon)) setIcon(iconChoices[0]);
  }, [iconChoices, icon]);

  // 只认最后一次请求的结果：连续点目录/打字时，旧响应必须落地不了
  const seq = useRef(0);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      // Esc 分层退出：先关目录面板，再关弹窗（一趟关掉两层会让人丢下手感）
      if (browsing) setBrowsing(false);
      else onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [browsing, onClose]);

  // 打开面板时才拉快捷入口 —— 用户不点「源文件夹」，就不该为目录结构发请求
  useEffect(() => {
    if (!browsing || roots.length > 0) return;
    void fetchWorkdirRoots()
      .then((rs) => {
        setRoots(rs);
        const first = rs.find((r) => r.exists);
        // 没有 current 才落默认值：用户可能已经先输了路径
        setCurrent((cur) => cur || first?.path || '');
      })
      .catch((e) => setError(messageOf(e, t('workdir.browseFailed'))));
  }, [browsing, roots.length, t]);

  // 浏览：`current` 或 `filter` 一变就列一次。打字时等 220ms ——
  // 每敲一个字列一次整目录，既慢又会让列表在眼前抖。
  useEffect(() => {
    if (!browsing || !current) return;
    const my = ++seq.current;
    const q = filter.trim();
    const id = window.setTimeout(() => {
      setBusyDir(true);
      browseWorkdir(current, q)
        .then((res) => {
          if (seq.current !== my) return;
          setBrowse(res);
          setError('');
        })
        .catch((e) => {
          if (seq.current !== my) return;
          setBrowse(null);
          setError(messageOf(e, t('workdir.browseFailed')));
        })
        .finally(() => {
          if (seq.current === my) setBusyDir(false);
        });
    }, q ? 220 : 0);
    return () => window.clearTimeout(id);
  }, [browsing, current, filter, t]);

  const selectDir = (path: string) => {
    setPicked(path);
    setBrowsing(false);
    if (!nameTouched) setName(basename(path));
    setError('');
  };

  // 系统对话框是模态的：开着的时候再点会叠出第二个窗口，所以拿 picking 挡住
  const pickNative = async () => {
    const bridge = window.xjmsDesktop;
    if (!bridge || picking) return;
    setPicking(true);
    try {
      const dir = await bridge.dialog.selectFolder(t('workdir.nativeTitle'));
      // 取消不是错误：什么都不填，让用户再点一次或改走手动输入
      if (dir) selectDir(dir);
    } catch (e) {
      setError(messageOf(e, t('workdir.nativeFailed')));
    } finally {
      setPicking(false);
    }
  };

  const submit = async () => {
    if (!picked || submitting) return;
    setSubmitting(true);
    try {
      const entry = await create({ path: picked, name: name.trim(), icon, color });
      try {
        await bind(threadId, entry.id);
        toast(t('workdir.createdBound', { name: entry.name }), 'success');
      } catch (e) {
        // 登记成功了但没绑上（会话 id 非法 / 并发中目录被删）。
        // 这只说一半就报错会让人以为「没建出来」，界面上其实就在列表里。
        toast(`${t('workdir.created')}${messageOf(e, t('workdir.bindFailed'))}`, 'error');
      }
      onClose();
    } catch (e) {
      setError(messageOf(e, t('workdir.createFailed')));
      setSubmitting(false);
    }
  };

  const ActiveIcon = useMemo(() => workdirIcon(icon), [icon]);

  // 面板已经展开时这颗只管收起来 —— 否则桌面端点它会隔着系统对话框再叠一层
  const onDropClick = () => {
    if (browsing) {
      setBrowsing(false);
      return;
    }
    if (native) void pickNative();
    else setBrowsing(true);
  };

  return (
    <div
      className="modal-mask"
      role="presentation"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && !submitting) onClose();
      }}
    >
      <div className="modal wdnew" role="dialog" aria-modal="true" aria-labelledby="wdnew-title">
        <div className="modal__head">
          <div className="wdnew__icon" style={{ color }}>
            <ActiveIcon size={18} />
          </div>
          <div style={{ flex: 1 }}>
            <div className="modal__title" id="wdnew-title">
              {t('workdir.newTitle')}
            </div>
          </div>
          <button type="button" className="wdnew__close" onClick={onClose} title={t('common.close')}>
            <X size={15} />
          </button>
        </div>

        <div className="modal__body wdnew__body">
          <div className="wdnew__field">
            <div className="wdnew__label">{t('workdir.sourceFolder')}</div>

            {picked ? (
              <div className="wdnew__picked">
                <FolderOpen size={14} />
                <span className="wdnew__picked-path" title={picked}>
                  {picked}
                </span>
                <button
                  type="button"
                  className="wdnew__link"
                  onClick={() => (native ? void pickNative() : setBrowsing(true))}
                >
                  {t('workdir.change')}
                </button>
                <button
                  type="button"
                  className="wdnew__link wdnew__link--muted"
                  onClick={() => {
                    setPicked('');
                    setNameTouched(false);
                    setName('');
                  }}
                  title={t('workdir.clearPick')}
                >
                  <X size={13} />
                </button>
              </div>
            ) : (
              <>
                <button
                  type="button"
                  className={`wdnew__drop${browsing ? ' is-open' : ''}`}
                  disabled={picking}
                  onClick={onDropClick}
                >
                  {picking ? <Loader size={15} className="spin" /> : <Folder size={15} />}
                  {picking || browsing ? t('workdir.pickFolderBusy') : t('workdir.pickFolder')}
                </button>
                {native && (
                  <button
                    type="button"
                    className="wdnew__link wdnew__manual"
                    onClick={() => setBrowsing(true)}
                  >
                    {t('workdir.pickManually')}
                  </button>
                )}
              </>
            )}

            {browsing && (
              <div className="wdnew__browser">
                <div className="wdnew__roots">
                  {roots.map((r, i) => (
                    <button
                      type="button"
                      key={r.path}
                      className={`wdnew__root${r.path === current ? ' is-active' : ''}`}
                      disabled={!r.exists}
                      title={r.path}
                      onClick={() => {
                        setFilter('');
                        setCurrent(r.path);
                      }}
                    >
                      {/* 后端第一格恒是「主目录」，给它一个房子；其余（桌面/文档/盘根）用目录名原样显示 */}
                      {i === 0 ? <Home size={11} /> : <Folder size={11} />}
                      {r.label}
                    </button>
                  ))}
                </div>

                <div className="wdnew__bar">
                  <input
                    className="wdnew__path-input"
                    value={current}
                    spellCheck={false}
                    aria-label={t('workdir.pathInputAria')}
                    title={t('workdir.pathInputTip')}
                    onChange={(e) => setCurrent(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') setFilter('');
                    }}
                  />
                  <button
                    type="button"
                    className="wdnew__up"
                    disabled={!browse?.parent}
                    title={t('workdir.upTip')}
                    onClick={() => {
                      if (browse?.parent) {
                        setFilter('');
                        setCurrent(browse.parent);
                      }
                    }}
                  >
                    <ArrowLeft size={13} />
                  </button>
                </div>

                <div className="wdnew__filter">
                  <Search size={12} />
                  <input
                    className="wdnew__filter-input"
                    value={filter}
                    placeholder={t('workdir.filterPlaceholder')}
                    spellCheck={false}
                    aria-label={t('workdir.filterPlaceholder')}
                    onChange={(e) => setFilter(e.target.value)}
                  />
                  {busyDir && <Loader size={12} className="spin" />}
                </div>

                <div className="wdnew__dirs">
                  {!browse ? (
                    /* 没结果时也要说句话：一片空白会被读成「这个目录是空的」，
                       而它其实是「还没拿到 / 拿不到」（错误详情在下面的红条里） */
                    <div className="wdnew__dirs-empty">
                      {busyDir ? t('common.loading') : t('workdir.browseFailed')}
                    </div>
                  ) : !browse.can_read ? (
                    <div className="wdnew__dirs-empty">{t('workdir.noAccess')}</div>
                  ) : browse.entries.length === 0 ? (
                    <div className="wdnew__dirs-empty">
                      {filter.trim() ? t('workdir.noSubMatch') : t('workdir.noSub')}
                    </div>
                  ) : (
                    browse.entries.map((entry) => (
                      <button
                        type="button"
                        key={entry.path}
                        className="wdnew__dir"
                        title={entry.path}
                        onClick={() => {
                          setFilter('');
                          setCurrent(entry.path);
                        }}
                      >
                        <Folder size={13} />
                        <span className="wdnew__dir-name">{entry.name}</span>
                        <ChevronRight size={12} className="wdnew__dir-go" />
                      </button>
                    ))
                  )}
                  {browse?.truncated && (
                    <div className="wdnew__truncated">
                      {t('workdir.truncated', { shown: browse.count, total: browse.total })}
                    </div>
                  )}
                </div>

                <button
                  type="button"
                  className="wdnew__choose"
                  disabled={!browse}
                  onClick={() => browse && selectDir(browse.path)}
                >
                  <Check size={13} />
                  {browse
                    ? t('workdir.chooseNamed', { path: basename(browse.path) })
                    : t('workdir.choose')}
                </button>
              </div>
            )}
          </div>

          <div className="wdnew__field">
            <label className="wdnew__label" htmlFor="wdnew-name">
              {t('workdir.name')}
            </label>
            <input
              id="wdnew-name"
              className="wdnew__input"
              value={name}
              maxLength={NAME_MAX}
              placeholder={picked ? basename(picked) : t('workdir.namePlaceholder')}
              onChange={(e) => {
                setNameTouched(true);
                setName(e.target.value);
              }}
            />
          </div>

          <div className="wdnew__field">
            <div className="wdnew__label">{t('workdir.icon')}</div>
            <div className="wdnew__icons">
              {iconChoices.map((key) => {
                const Icon = workdirIcon(key);
                const active = key === icon;
                return (
                  <button
                    type="button"
                    key={key}
                    className={`wdnew__icon-btn${active ? ' is-active' : ''}`}
                    style={active ? { color, borderColor: color } : undefined}
                    aria-pressed={active}
                    title={key}
                    onClick={() => setIcon(key)}
                  >
                    <Icon size={15} />
                  </button>
                );
              })}
            </div>
          </div>

          <div className="wdnew__field">
            <div className="wdnew__label">{t('workdir.color')}</div>
            <div className="wdnew__colors">
              {COLORS.map((c) => (
                <button
                  type="button"
                  key={c}
                  className={`wdnew__swatch${c === color ? ' is-active' : ''}`}
                  style={{ background: c }}
                  aria-pressed={c === color}
                  title={c}
                  onClick={() => setColor(c)}
                />
              ))}
            </div>
          </div>

          {error && <div className="wdnew__error">{error}</div>}
        </div>

        <div className="modal__foot">
          <button type="button" className="btn" onClick={onClose} disabled={submitting}>
            {t('common.cancel')}
          </button>
          <button
            type="button"
            className="btn btn--primary"
            onClick={() => void submit()}
            disabled={!picked || submitting}
            title={t('workdir.authorizeNote')}
          >
            {submitting && <span className="spinner" />}
            {t('workdir.createConfirm')}
          </button>
        </div>
      </div>
    </div>
  );
}
