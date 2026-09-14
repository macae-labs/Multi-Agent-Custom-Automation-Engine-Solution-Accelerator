/**
 * Stub de `@monaco-editor/react` para vitest (ver monaco-editor.ts).
 */
import type { FC } from 'react';

export const loader = {
  config: (): void => undefined,
  init: (): Promise<unknown> => Promise.resolve({}),
};

export const Editor: FC<Record<string, unknown>> = () => (
  <div data-testid="monaco-editor-stub" />
);

export const DiffEditor: FC<Record<string, unknown>> = () => (
  <div data-testid="monaco-diff-editor-stub" />
);

export type OnMount = (...args: unknown[]) => void;

export default Editor;
