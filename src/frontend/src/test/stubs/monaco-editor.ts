/**
 * Stub de `monaco-editor` para vitest. jsdom no puede cargar el ESM real
 * (falla al parsear su CSS y al registrar comandos en monaco-lsp-client), y
 * cualquier test que importe —aunque sea de forma transitiva— un componente
 * que use Monaco (StreamingAgentMessage → HtmlPreview → WorkspaceEditor)
 * moría en el import. Cubre exactamente lo que monacoSetup/WorkspaceEditor
 * tocan al importar. El editor real se valida en Chrome (harness Playwright).
 */
export const MarkerSeverity = { Hint: 1, Info: 2, Warning: 4, Error: 8 };

export const editor = {
  defineTheme: (): void => undefined,
  setModelMarkers: (): void => undefined,
  getModelMarkers: (): unknown[] => [],
  getModels: (): unknown[] => [],
};

export const languages = {
  getLanguages: (): Array<{ id: string }> => [],
};

const defaults = { setDiagnosticsOptions: (): void => undefined };
export const typescript = {
  typescriptDefaults: defaults,
  javascriptDefaults: defaults,
};

/** `import x from 'monaco-editor/.../*.worker?worker'` → constructor de Worker. */
export default class StubWorker {}
