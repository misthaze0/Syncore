/**
 * 外观设置 —— 主题 / 字号 / 对话壁纸 / 输入框样式
 *
 * 参照 Linear「Appearance」与 ChatGPT「General」：视觉偏好集中在这一页，
 * 控件用分段选择器，机制说明只进 tooltip。页面语言与助理页一致。
 *
 * 壁纸全是用户自己上传的图，可以存多张、点哪张用哪张：
 * 选项之间的差别就是画面本身，用文字描述不如让人一眼看见它。
 * 输入框样式同理，而且更必要 —— 这个弹窗正好盖在输入框上面，
 * 不画小样就只能凭名字猜「玫瑰荆棘」到底长什么样。
 *
 * ★ 自定义图的比例要在上传时定，不是上传后调
 *
 * 「对话区是横的」这件事用户不一定知道，等他看见裁掉半张脸再回头调锚点，
 * 只会更恼火。所以上传弹窗里就有比例清单，这一页只留铺法、位置、浓度三个后续可调项
 * —— 而且前两个是「这一张」的设置，不是全局的：换一张图不该继承上一张的铺法。
 */

import { useRef, useState, type CSSProperties } from 'react';

import { CustomWallpaperDialog, type CustomWallpaperResult } from '@/components/settings/CustomWallpaperDialog';
import { Check, Plus, X } from '@/components/icons';
import { COMPOSER_STYLES, useComposerStyleStore } from '@/hooks/useComposerStyle';
import { FONT_LABEL, useFontStore, type FontLevel } from '@/hooks/useFontSize';
import { useThemeStore } from '@/hooks/useTheme';
import { THEMES } from '@/lib/theme';
import { useT } from '@/i18n';
import {
  CUSTOM_FIT_OPTIONS,
  MAX_WALLPAPER_ITEMS,
  useWallpaperStore,
  type HAlign,
  type VAlign,
} from '@/hooks/useWallpaper';
import { formatBytes } from '@/lib/customWallpaper';

import { PageHead, Row, Rows, Section, Switch } from '@/components/settings2/kit';

const FONT_OPTIONS: FontLevel[] = ['s', 'm', 'l', 'xl'];

const H_ALIGN: HAlign[] = ['left', 'center', 'right'];
const V_ALIGN: VAlign[] = ['top', 'center', 'bottom'];

/** 卡片上的名字：文件名去掉扩展名，再长就截 —— 缩略图旁边放不下目录 */
function displayName(file: File): string {
  const stem = file.name.replace(/\.[^./\\]+$/, '');
  const short = stem.trim() || file.name.trim();
  return short.length > 14 ? `${short.slice(0, 13)}…` : short || '壁纸';
}

export function AppearancePanel() {
  const t = useT();
  const preference = useThemeStore((s) => s.preference);
  const theme = useThemeStore((s) => s.theme);
  const setPreference = useThemeStore((s) => s.setPreference);
  const fontLevel = useFontStore((s) => s.level);
  const setFontLevel = useFontStore((s) => s.setLevel);
  const wallpaper = useWallpaperStore((s) => s.wallpaper);
  const setWallpaper = useWallpaperStore((s) => s.setWallpaper);
  const strength = useWallpaperStore((s) => s.strength);
  const setStrength = useWallpaperStore((s) => s.setStrength);
  const items = useWallpaperStore((s) => s.items);
  const addWallpaper = useWallpaperStore((s) => s.addWallpaper);
  const removeWallpaper = useWallpaperStore((s) => s.removeWallpaper);
  const setItemFit = useWallpaperStore((s) => s.setItemFit);
  const setItemAnchor = useWallpaperStore((s) => s.setItemAnchor);
  const composerStyle = useComposerStyleStore((s) => s.composerStyle);
  const setComposerStyle = useComposerStyleStore((s) => s.setComposerStyle);

  const fileRef = useRef<HTMLInputElement | null>(null);
  /** 一次多选进来几张就排几个队，弹窗一张张过；取消 = 跳过这一张，不是中止整批 */
  const [queue, setQueue] = useState<File[]>([]);
  const [note, setNote] = useState('');

  const selected = items.find((item) => item.id === wallpaper) ?? null;
  const usedBytes = items.reduce((sum, item) => sum + item.bytes, 0);

  const onPick = (files: FileList | null) => {
    setNote('');
    const picked = Array.from(files ?? []);
    if (picked.length) setQueue(picked);
  };

  const onResolve = (result: CustomWallpaperResult | null) => {
    const [current, ...rest] = queue;
    setQueue(rest);
    if (!result || !current) return;
    const added = addWallpaper({
      src: result.dataUrl,
      fit: result.fit,
      bytes: result.bytes,
      name: displayName(current),
    });
    if (!added.ok) {
      setNote(
        added.reason === 'full'
          ? `最多存 ${MAX_WALLPAPER_ITEMS} 张，先移除一张`
          : `这张压完 ${formatBytes(result.bytes)}，本地空间不够 —— 先移除一张再传`,
      );
      return;
    }
    setNote(`已设为对话背景 · ${formatBytes(result.bytes)}`);
  };

  return (
    <div className="tset tset-page">
      <PageHead title="外观" />

      <Section title="主题">
        <div className="theme-choices" role="radiogroup" aria-label="界面主题">
          {THEMES.map(value => <label key={value} className={`theme-choice${preference === value ? ' is-selected' : ''}`}>
            <input type="radio" name="interface-theme" value={value} checked={preference === value} onChange={() => setPreference(value)} aria-label={t(`theme.${value}`)} />
            <span className={`theme-preview theme-preview--${value}`} aria-hidden="true"><span className="theme-preview__sidebar"><i /><i /><i /></span><span className="theme-preview__main"><i /><i /><span /></span></span>
            <span className="theme-choice__label">{t(`theme.${value}`)}{preference === value && <Check size={13} />}</span>
            <span className="theme-choice__hint">{t(`theme.${value}Hint`)}</span>
          </label>)}
        </div>
        <Rows>
          <Row
            label="跟随系统"
            desc="自动切换浅色与暗色；选择主题可关闭。"
            control={<Switch checked={preference === 'system'} onChange={enabled => setPreference(enabled ? 'system' : theme)} ariaLabel="跟随系统主题" />}
          />
        </Rows>
      </Section>

      <Section title="字号">
        <Rows>
          <Row
            label="对话字号"
            control={
              <div className="seg" role="radiogroup" aria-label="对话字号">
                {FONT_OPTIONS.map((level) => (
                  <button
                    key={level}
                    type="button"
                    className={`seg__btn${fontLevel === level ? ' seg__btn--on' : ''}`}
                    onClick={() => setFontLevel(level)}
                    aria-pressed={fontLevel === level}
                  >
                    {FONT_LABEL[level]}
                  </button>
                ))}
              </div>
            }
          />
        </Rows>
      </Section>

      <Section title="壁纸">
        <Rows>
          <Row
            stack
            label="对话背景"
            control={
              <div className="wp-grid" role="radiogroup" aria-label="对话背景">
                <button
                  type="button"
                  className={`wp-card${wallpaper === 'none' ? ' wp-card--on' : ''}`}
                  onClick={() => setWallpaper('none')}
                  aria-pressed={wallpaper === 'none'}
                >
                  <span className="wp-card__thumb wp-card__thumb--none" />
                  <span className="wp-card__label">无</span>
                </button>
                {items.map((item) => (
                  <div key={item.id} className={`wp-card wp-card--item${wallpaper === item.id ? ' wp-card--on' : ''}`}>
                    <button
                      type="button"
                      className="wp-card__pick"
                      onClick={() => setWallpaper(item.id)}
                      aria-pressed={wallpaper === item.id}
                      title={`${item.name} · ${formatBytes(item.bytes)}`}
                    >
                      <span
                        className="wp-card__thumb"
                        style={{
                          backgroundImage: `url("${item.src}")`,
                          // 缩略图照这一张自己的铺法与锚点画：改了「位置」，卡片上就该立刻看见
                          backgroundSize: item.fit === 'repeat' ? 'auto' : item.fit,
                          backgroundPosition: item.fit === 'repeat' ? '0 0' : `${item.anchor.h} ${item.anchor.v}`,
                          backgroundRepeat: item.fit === 'repeat' ? 'repeat' : 'no-repeat',
                        }}
                      />
                      <span className="wp-card__label">{item.name}</span>
                    </button>
                    <button
                      type="button"
                      className="wp-card__del"
                      title="从图库移除"
                      aria-label={`移除 ${item.name}`}
                      onClick={() => {
                        removeWallpaper(item.id);
                        setNote('');
                      }}
                    >
                      <X size={12} />
                    </button>
                  </div>
                ))}
                <button
                  type="button"
                  className="wp-card wp-card--add"
                  onClick={() => fileRef.current?.click()}
                  title="上传自己的图片，可一次选择多张"
                >
                  <span className="wp-card__thumb wp-card__thumb--empty">
                    <Plus size={16} />
                  </span>
                  <span className="wp-card__label">上传图片</span>
                </button>
              </div>
            }
          />

          {selected && (
            <>
              <Row
                label="填充方式"
                title="只对当前这张生效"
                control={
                  <div className="seg" role="radiogroup" aria-label="铺法">
                    {CUSTOM_FIT_OPTIONS.map((opt) => (
                      <button
                        key={opt.value}
                        type="button"
                        className={`seg__btn${selected.fit === opt.value ? ' seg__btn--on' : ''}`}
                        onClick={() => setItemFit(selected.id, opt.value)}
                        aria-pressed={selected.fit === opt.value}
                      >
                        {opt.label}
                      </button>
                    ))}
                  </div>
                }
              />

              {selected.fit !== 'repeat' && (
                <Row
                  label="位置"
                  title="只对当前这张生效"
                  control={
                    <div className="wp-anchor" role="radiogroup" aria-label="画面位置">
                      {V_ALIGN.map((v) =>
                        H_ALIGN.map((h) => (
                          <button
                            key={`${h}-${v}`}
                            type="button"
                            className={`wp-anchor__cell${
                              selected.anchor.h === h && selected.anchor.v === v ? ' is-on' : ''
                            }`}
                            aria-pressed={selected.anchor.h === h && selected.anchor.v === v}
                            onClick={() => setItemAnchor(selected.id, { h, v })}
                            title={`${v} ${h}`}
                          />
                        )),
                      )}
                    </div>
                  }
                />
              )}
            </>
          )}

          {wallpaper !== 'none' && (
            <Row
              label="壁纸不透明度"
              control={
                <>
                  <input
                    type="range"
                    className="wp-range"
                    min={0}
                    max={100}
                    step={5}
                    value={strength}
                    onChange={(e) => setStrength(Number(e.target.value))}
                    style={{ '--wp-fill': `${strength}%` } as CSSProperties}
                    aria-label="壁纸不透明度"
                  />
                  <span className="wp-range__value">{strength}%</span>
                </>
              }
            />
          )}

          <Row
            label="图库"
            title={`已存 ${items.length} / ${MAX_WALLPAPER_ITEMS} 张 · 共 ${formatBytes(usedBytes)}`}
            control={
              <>
                <input
                  ref={fileRef}
                  type="file"
                  accept="image/*"
                  multiple
                  className="wp-custom__input"
                  onChange={(e) => {
                    onPick(e.target.files);
                    e.target.value = '';
                  }}
                />
                <span className="badge badge--muted">
                  {items.length} / {MAX_WALLPAPER_ITEMS}
                </span>
                <button type="button" className="btn btn--sm" onClick={() => fileRef.current?.click()}>
                  上传图片
                </button>
              </>
            }
          />
        </Rows>
        {note && (
          <div className={`wp-custom__note${note.startsWith('已') ? '' : ' is-bad'}`}>{note}</div>
        )}
      </Section>

      <Section title="输入框">
        <Rows>
          <Row
            stack
            label="输入框样式"
            control={
              <div className="cs-grid" role="radiogroup" aria-label="输入框样式">
                {COMPOSER_STYLES.map((opt) => (
                  <button
                    key={opt.id}
                    type="button"
                    className={`cs-card${composerStyle === opt.id ? ' cs-card--on' : ''}`}
                    onClick={() => setComposerStyle(opt.id)}
                    aria-pressed={composerStyle === opt.id}
                    title={opt.hint}
                  >
                    <span className={`cs-pv${opt.id === 'rose' ? ' cs-pv--rose' : ''}`}>
                      <span className="cs-pv__text" />
                      <span className="cs-pv__acts">
                        <span className="cs-pv__dot" />
                        <span className="cs-pv__dot cs-pv__dot--send" />
                      </span>
                    </span>
                    <span className="cs-card__label">{opt.label}</span>
                  </button>
                ))}
              </div>
            }
          />
        </Rows>
      </Section>

      {queue[0] && (
        <CustomWallpaperDialog file={queue[0]} remaining={queue.length - 1} onResolve={onResolve} />
      )}
    </div>
  );
}

export default AppearancePanel;
