// SSE hook for the live negotiation stream (/stream/{id}).
// Subscribe after POST /negotiate_text {stream:true} returns a stream_id.

import { API_BASE } from './api';

export type WalletOp = 'seed' | 'debit' | 'gate_blocked' | 'credit';

/** A single SSE frame. `type` discriminates; payload lives in `data`. */
export interface StreamEvent {
  seq?: number;
  ts_ms?: number;
  type: string;          // agent | layer | status | verdict | phase | wallet | risk | emergency | narrate | …
  agent?: string;
  round?: number | null;
  summary?: string;
  data?: Record<string, unknown> & { op?: WalletOp };
  result?: Record<string, unknown>;   // present on the final negotiate_finished frame (the FULL plan envelope)
  [k: string]: unknown;
}

export interface StreamHandle { close: () => void; }

/**
 * Subscribe to the live event stream. Returns a handle; call close() to stop.
 * Pure transport — does not interpret events (the UI layer maps them).
 */
export function openStream(
  streamId: string,
  onEvent: (e: StreamEvent) => void,
  onError?: (err: Event) => void,
): StreamHandle {
  const es = new EventSource(`${API_BASE}/stream/${encodeURIComponent(streamId)}`);
  es.onmessage = (m: MessageEvent) => {
    try {
      onEvent(JSON.parse(m.data) as StreamEvent);
    } catch {
      /* ignore a malformed frame rather than tearing down the stream */
    }
  };
  if (onError) es.onerror = onError;
  return { close: () => es.close() };
}
