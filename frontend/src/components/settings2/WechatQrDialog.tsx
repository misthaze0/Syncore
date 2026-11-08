/**
 * 微信扫码登录弹窗
 *
 * 只做一件事：把后端给的那张二维码摊开，然后老实说现在走到哪一步。
 *
 * ★ 为什么状态是一句一句报，而不是一个转圈的 spinner
 * iLink 的扫码过程本来就有五个不同的中途状态（没扫 / 扫了没确认 / 要验证码 /
 * 过期 / 这个号早绑过），它们在**用户那边**要做的动作完全不同。合成一个
 * 「加载中」会让人举着手机不知道该干什么 —— 尤其是「要验证码」那一步，
 * 数字只出现在手机屏幕上，不填进来就一直卡着。
 *
 * ★ 为什么关掉窗口要通知后端
 * 后端的轮询是条 35s 长轮询，弹窗关了还让它一直问到 5 分钟超时，等于白占一条
 * 对网关的连接。作废是礼貌，也是给下一次「连接」腾出干净状态。
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import { QrCode, X } from '@/components/icons';
import {
  cancelWechatLogin,
  fetchWechatLogin,
  startWechatLogin,
  submitWechatVerifyCode,
} from '@/lib/api';
import type { WechatLoginState } from '@/types';

/** 轮询快照的节奏：状态只存在后端内存里，刷快一点不花钱 */
const POLL_MS = 1500;

const ACTIVE = new Set(['waiting', 'scanned', 'verifycode']);

function reasonText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

export function WechatQrDialog({
  open,
  onClose,
  onBound,
}: {
  open: boolean;
  onClose: () => void;
  /** 绑定成功时回调，让父层去刷渠道状态（顶栏那颗点要跟着变） */
  onBound: () => void;
}) {
  const [state, setState] = useState<WechatLoginState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [code, setCode] = useState('');
  /** 过期后「重新获取」靠它踢一下 effect */
  const [nonce, setNonce] = useState(0);
  const onBoundRef = useRef(onBound);
  onBoundRef.current = onBound;

  useEffect(() => {
    if (!open) return;
    let alive = true;
    let timer = 0;
    const stop = () => window.clearInterval(timer);
    const apply = (s: WechatLoginState) => {
      if (!alive) return;
      setState(s);
      if (s.status === 'confirmed') {
        stop();
        onBoundRef.current();
      } else if (!ACTIVE.has(s.status)) {
        stop();
      }
    };

    setError(null);
    setState(null);
    setCode('');
    startWechatLogin().then(apply).catch((e) => alive && setError(reasonText(e)));
    timer = window.setInterval(() => {
      fetchWechatLogin()
        .then(apply)
        .catch(() => {
          /* 一次问不到不算什么，下一轮自愈；这里报错只会让弹窗抖一下 */
        });
    }, POLL_MS);

    return () => {
      alive = false;
      stop();
      void cancelWechatLogin();
    };
  }, [open, nonce]);

  // Esc 只关这一层。设置弹窗的 Esc 是挂在 window 上的冒泡监听，
  // 这里用捕获阶段先吃掉并 stopPropagation，否则按一下 Esc 两层一起没。
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      e.stopPropagation();
      onClose();
    };
    window.addEventListener('keydown', onKey, { capture: true });
    return () => window.removeEventListener('keydown', onKey, { capture: true });
  }, [open, onClose]);

  const sendCode = useCallback(() => {
    const v = code.trim();
    if (!v) return;
    submitWechatVerifyCode(v)
      .then(setState)
      .catch((e) => setError(reasonText(e)));
  }, [code]);

  if (!open) return null;

  const status = state?.status ?? 'waiting';
  const busy = !state && !error;

  return (
    <div
      className="modal-mask"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
      role="presentation"
    >
      <div className="modal qr-dialog" role="dialog" aria-modal="true" aria-label="微信扫码">
        <div className="modal__head">
          <div className="modal__icon">
            <QrCode size={19} />
          </div>
          <div>
            <div className="modal__title">微信助理</div>
          </div>
          <button type="button" className="btn btn--sm btn--ghost qr-dialog__close" onClick={onClose}>
            <X size={14} />
          </button>
        </div>

        <div className="modal__body">
          {error ? (
            <div className="qr-dialog__note qr-dialog__note--bad">{error}</div>
          ) : (
            <>
              <div className="qr-dialog__box">
                {busy ? (
                  <span className="qr-dialog__loading">正在取二维码…</span>
                ) : state?.qr_png ? (
                  <img src={state.qr_png} alt="微信登录二维码" width={208} height={208} />
                ) : (
                  <span className="qr-dialog__loading">这张码已经不能扫了</span>
                )}
              </div>
              <StatusLine state={state} />
              {status === 'verifycode' && (
                <div className="qr-dialog__verify">
                  <input
                    className="input qr-dialog__code"
                    value={code}
                    inputMode="numeric"
                    autoComplete="off"
                    placeholder="手机微信上的数字"
                    onChange={(e) => setCode(e.target.value.slice(0, 8))}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') sendCode();
                    }}
                  />
                  <button type="button" className="btn btn--sm btn--primary" onClick={sendCode}>
                    提交
                  </button>
                </div>
              )}
            </>
          )}
        </div>

        <div className="modal__foot">
          {(status === 'expired' || status === 'failed' || error) && (
            <button
              type="button"
              className="btn btn--sm"
              onClick={() => setNonce((n) => n + 1)}
              title="重新向后端要一张二维码"
            >
              重新获取
            </button>
          )}
          <button
            type="button"
            className="btn btn--sm btn--primary"
            onClick={onClose}
            title={
              status === 'confirmed'
                ? '绑定凭据已存在这台电脑上'
                : '关掉即作废，不算绑定'
            }
          >
            {status === 'confirmed' ? '完成' : '关闭'}
          </button>
        </div>
      </div>
    </div>
  );
}

/** 状态行：一句话说清「现在该谁动」，机制细节留在 title 里 */
function StatusLine({ state }: { state: WechatLoginState | null }) {
  const status = state?.status ?? 'waiting';
  const left = state?.expires_in ?? 0;

  if (status === 'confirmed') {
    return <div className="qr-dialog__note qr-dialog__note--ok">绑定好了，微信里可以直接找它了</div>;
  }
  if (status === 'bound') {
    return (
      <div className="qr-dialog__note" title={state?.detail}>
        这个微信号之前已经绑过，不用重复扫
      </div>
    );
  }
  if (status === 'expired') {
    return <div className="qr-dialog__note">二维码过期了，点「重新获取」再来一张</div>;
  }
  if (status === 'failed') {
    return <div className="qr-dialog__note qr-dialog__note--bad">{state?.detail || '这次没成，重来一次'}</div>;
  }
  if (status === 'scanned') {
    return (
      <div className="qr-dialog__note qr-dialog__note--ok" title="手机微信上点了「确认登录」之后才会继续">
        已扫码 —— 在手机上确认一下
      </div>
    );
  }
  if (status === 'verifycode') {
    return (
      <div className="qr-dialog__note qr-dialog__note--warn" title="微信的安全校验，数字只显示在你手机上">
        手机微信显示了一个数字，填进来
      </div>
    );
  }
  return (
    <div
      className="qr-dialog__note"
      title="扫完不会立刻生效：微信那头要点确认，后端拿到凭据才算绑定"
    >
      等待扫码{left > 0 ? ` · 还剩 ${left} 秒` : ''}
    </div>
  );
}

export default WechatQrDialog;
