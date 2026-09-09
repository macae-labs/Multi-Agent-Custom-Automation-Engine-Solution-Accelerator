// Shared OAuth-consent store for MCP servers that require an interactive sign-in.
//
// The backend streams an `oauth_consent_request` (with the Clerk/authorize URL)
// when connecting an MCP server needs the user to log in. A popup MUST be opened
// from a real user gesture — a browser blocks `window.open` called straight from
// the async SSE handler — so instead of auto-opening we publish the request here
// and a single <OAuthConsentDialog> renders a button the user clicks. Completion
// is driven by the `window.opener.postMessage({type:'mcp_oauth', ok})` the backend
// callback page already emits, then the original chat message is retried.
import { useSyncExternalStore } from 'react';

export interface OAuthConsentRequest {
  link: string;
  serverName?: string;
  retry: () => void;
}

let current: OAuthConsentRequest | null = null;
const listeners = new Set<() => void>();

function emit(): void {
  listeners.forEach((l) => l());
}

/** Ask the UI to prompt the user to sign in, then re-run `retry` on success. */
export function requestOAuthConsent(
  link: string,
  retry: () => void,
  serverName?: string
): void {
  current = { link, retry, serverName };
  emit();
}

export function clearOAuthConsent(): void {
  current = null;
  emit();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function getSnapshot(): OAuthConsentRequest | null {
  return current;
}

export function useOAuthConsent(): OAuthConsentRequest | null {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}
