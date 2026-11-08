/**
 * SSE 解析工具
 *
 * 用 fetch + ReadableStream 手写解析，而不是 EventSource —— 因为聊天接口是 POST，
 * 且需要支持 AbortController 主动中断。
 *
 * ★ 为什么必须逐行解析，而不能按 '\n\n' 切块
 *
 * SSE 规范（WHATWG HTML「Interpreting an event stream」）规定：行分隔符可以是
 * CRLF、单个 LF 或单个 CR 三者之一，而**空行**才代表一个事件结束。
 * 也就是说，「事件块之间的那个空行」究竟长什么样，取决于服务端选用的行分隔符。
 *
 * 本项目后端用 sse-starlette，它的 DEFAULT_SEPARATOR 正是 '\r\n'（也是规范推荐值），
 * 所以线上的真实字节是 `data: {...}\r\n\r\n`。若客户端只找 '\n\n'：
 *   '\r' '\n' '\r' '\n' 里并不存在相邻的两个 '\n' —— 边界永远匹配不上，
 *   整条流会全部堆在 buffer 里，最后被当成**一个**超长 data 去 JSON.parse，
 *   解析必然失败（控制台报「无法解析的事件块」），从而一个事件都拿不到。
 *   表现就是「后端日志里 token 明明在流，前端气泡却永远是空的」。
 *
 * 所以这里严格按规范逐行解析：三种行尾都认、空行才派发、':' 开头的行是注释
 * （sse-starlette 每 15s 注入一行 `: ping` 心跳，正好按注释规则被忽略）。
 */

import type { AgentEvent } from '@/types';

/** 任意一种 SSE 行分隔符：CRLF / LF / CR */
const LINE_BREAK = /\r\n|\r|\n/;

/** 把 fetch Response 的字节流解析为事件序列（默认按 AgentEvent，可指定别的载荷类型） */
export async function* parseSSE<T = AgentEvent>(response: Response): AsyncGenerator<T, void, void> {
  if (!response.body) return;

  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8');

  let tail = '';                 // 尚未凑成完整一行的残留
  let dataLines: string[] = [];  // 当前事件块累积的 data 行

  /** 派发当前事件块（规范：data 为空则不派发） */
  function* dispatch(): Generator<T, void, void> {
    if (dataLines.length === 0) return;

    const payload = dataLines.join('\n');
    dataLines = [];
    if (!payload) return;

    try {
      yield JSON.parse(payload) as T;
    } catch {
      console.warn('[sse] 无法解析的事件块:', payload);
    }
  }

  /** 处理一整行（规范中的「process the field」） */
  function* handleLine(line: string): Generator<T, void, void> {
    // 空行 = 一个事件结束
    if (line === '') {
      yield* dispatch();
      return;
    }
    // ':' 开头是注释（心跳 ping），忽略
    if (line.startsWith(':')) return;

    const colon = line.indexOf(':');
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? '' : line.slice(colon + 1);
    // 规范：冒号后若紧跟一个空格，去掉这一个
    if (value.startsWith(' ')) value = value.slice(1);

    if (field === 'data') dataLines.push(value);
  }

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;

      tail += decoder.decode(value, { stream: true });

      for (;;) {
        const match = LINE_BREAK.exec(tail);
        if (!match) break;
        // 末尾孤立的 '\r' 可能只是 '\r\n' 的前半截，留给下一个分片判断
        if (match[0] === '\r' && match.index === tail.length - 1) break;

        const line = tail.slice(0, match.index);
        tail = tail.slice(match.index + match[0].length);
        yield* handleLine(line);
      }
    }

    // 收尾：处理未以换行结束的残留，以及最后一个尚未派发的事件块
    tail += decoder.decode();
    if (tail !== '') yield* handleLine(tail);
    yield* dispatch();
  } finally {
    reader.releaseLock();
  }
}