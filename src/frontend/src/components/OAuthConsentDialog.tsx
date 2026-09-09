// Renders the sign-in prompt for an MCP server that requires OAuth. Mounted once
// (see App.tsx). Opening the popup happens ON THE BUTTON CLICK (a user gesture),
// which is the only way a browser reliably allows window.open — the previous
// auto-open from the async SSE handler was silently blocked. Completion is driven
// by the postMessage the backend OAuth callback page emits to window.opener.
import { useEffect, useRef, useState } from 'react';
import {
  Dialog,
  DialogSurface,
  DialogBody,
  DialogTitle,
  DialogContent,
  DialogActions,
  Button,
  Spinner,
} from '@fluentui/react-components';
import { useOAuthConsent, clearOAuthConsent } from '../utils/oauthConsent';

export function OAuthConsentDialog(): JSX.Element | null {
  const consent = useOAuthConsent();
  const [waiting, setWaiting] = useState(false);
  const [failed, setFailed] = useState(false);
  const popupRef = useRef<Window | null>(null);

  useEffect(() => {
    // Reset transient UI state whenever a new consent request arrives.
    setWaiting(false);
    setFailed(false);
  }, [consent?.link]);

  useEffect(() => {
    if (!consent) return;
    const onMessage = (e: MessageEvent) => {
      if (!e?.data || e.data.type !== 'mcp_oauth') return;
      if (popupRef.current && e.source !== popupRef.current) return;
      if (typeof (e.data as { ok?: unknown }).ok !== 'boolean') return;
      try {
        popupRef.current?.close();
      } catch {
        /* cross-origin close is best-effort */
      }
      if (e.data.ok) {
        const retry = consent.retry;
        clearOAuthConsent();
        retry();
      } else {
        setWaiting(false);
        setFailed(true);
      }
    };
    window.addEventListener('message', onMessage);
    return () => window.removeEventListener('message', onMessage);
  }, [consent]);

  if (!consent) return null;

  const startLogin = () => {
    setFailed(false);
    setWaiting(true);
    const popup = window.open(
      consent.link,
      'mcp_oauth',
      'width=620,height=720'
    );
    popupRef.current = popup;

    if (popup) {
      const timer = window.setInterval(() => {
        if (popup.closed) {
          window.clearInterval(timer);
          setWaiting(false);
          setFailed(true);
        }
      }, 500);
    }

    // If the popup is blocked even on a click (rare), fall back to same-tab nav
    // so the sign-in still proceeds; the callback returns to the app afterwards.
    if (!popup) {
      window.location.assign(consent.link);
    }
  };

  const label = consent.serverName ? `“${consent.serverName}”` : 'este servidor';

  return (
    <Dialog open modalType="alert">
      <DialogSurface>
        <DialogBody>
          <DialogTitle>Iniciar sesión para conectar</DialogTitle>
          <DialogContent>
            {failed
              ? `No se completó el inicio de sesión con ${label}. Inténtalo de nuevo.`
              : waiting
              ? 'Completa el inicio de sesión en la ventana que se abrió. Al autorizar, la conexión continúa sola.'
              : `Para conectar ${label} necesitas iniciar sesión. Se abrirá una ventana segura para autorizar.`}
          </DialogContent>
          <DialogActions>
            <Button
              appearance="secondary"
              onClick={() => {
                try {
                  popupRef.current?.close();
                } catch {
                  /* best-effort */
                }
                clearOAuthConsent();
              }}
            >
              Cancelar
            </Button>
            <Button
              appearance="primary"
              icon={waiting && !failed ? <Spinner size="tiny" /> : undefined}
              disabled={waiting && !failed}
              onClick={startLogin}
            >
              {failed ? 'Reintentar' : waiting ? 'Esperando…' : 'Iniciar sesión'}
            </Button>
          </DialogActions>
        </DialogBody>
      </DialogSurface>
    </Dialog>
  );
}
