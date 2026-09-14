/**
 * Harness del explorador de workspace — NO forma parte de la app.
 *
 * Reproduce el ciclo de vida real de HtmlPreview.PreviewRightSlot: el árbol
 * (WorkspaceTree) sólo existe en el slot de reposo; abrir un archivo lo
 * DESMONTA y monta WorkspaceEditor; cerrar el archivo lo REMONTA. El backend
 * lo falsea Playwright por interceptación de rutas y cuenta cada request.
 *
 * Expone en window.__ws lo que el script de contrato necesita para provocar
 * eventos que la UI no ofrece directamente (reconcile de ChatService,
 * invalidación por path, lectura de markers de Monaco).
 */
import {
  Button,
  FluentProvider,
  teamsLightTheme,
} from '@fluentui/react-components';
import React, { useCallback, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { apiClient } from '../src/api/apiClient';
import { WorkspaceEditor } from '../src/components/content/WorkspaceEditor';
import { monaco } from '../src/components/content/monacoSetup';
import { WorkspaceTree } from '../src/components/workspace/WorkspaceTree';
import { workspaceTree } from '../src/components/workspace/workspaceTreeStore';
import ChatService from '../src/services/ChatService';

const WS = 'ws-harness';

const Harness: React.FC = () => {
  const [open, setOpen] = useState<{ path: string; content: string } | null>(
    null
  );

  // Mismo camino que HtmlPreview.openWsFile: GET /files/{path} y luego el
  // editor recibe title/content/lang (lang = extensión cruda, como en la app).
  const openFile = useCallback(async (path: string) => {
    const encoded = path.split('/').map(encodeURIComponent).join('/');
    const f: { content?: string } = await apiClient.get(
      `/v4/workspace/${encodeURIComponent(WS)}/files/${encoded}`
    );
    setOpen({ path, content: f.content ?? '' });
  }, []);

  const close = useCallback(() => setOpen(null), []);

  (window as any).__ws = {
    ws: WS,
    store: workspaceTree,
    monaco,
    ChatService,
    openFile,
    close,
  };

  if (!open) {
    return (
      <div data-testid="resting-slot">
        <WorkspaceTree workspaceId={WS} onOpenFile={(p) => void openFile(p)} />
      </div>
    );
  }
  return (
    <div data-testid="editor-slot" style={{ height: '100vh' }}>
      <div style={{ padding: 4 }}>
        <Button size="small" aria-label="Cerrar archivo" onClick={close}>
          Cerrar {open.path}
        </Button>
      </div>
      <div style={{ height: 'calc(100vh - 40px)' }}>
        <WorkspaceEditor
          title={open.path}
          content={open.content}
          lang={(open.path.split('.').pop() || '').toLowerCase()}
          workspaceId={WS}
        />
      </div>
    </div>
  );
};

createRoot(document.getElementById('root')!).render(
  <FluentProvider theme={teamsLightTheme} style={{ height: '100vh' }}>
    <Harness />
  </FluentProvider>
);
