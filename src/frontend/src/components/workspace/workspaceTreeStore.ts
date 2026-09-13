/**
 * workspaceTreeStore — estado del explorador por workspace, FUERA del árbol
 * de React.
 *
 * Por qué: el árbol vive en el "resting slot" del panel de artefactos y se
 * DESMONTA cada vez que se abre un archivo. Con el estado dentro del
 * componente, cada cierre de tab remontaba el árbol → efecto de arranque →
 * GET /entries → expansión perdida. El remount era el mecanismo de coherencia,
 * es decir, lifecycle de UI mezclado con consistencia del filesystem.
 *
 * Aquí el estado (niveles cargados + expansión) persiste entre montajes, y la
 * coherencia con Azure Files se obtiene por invalidación EXPLÍCITA:
 *   - invalidate(ws, dir)   → un nivel (mutación conocida: create/delete/rename/save)
 *   - invalidateAll(ws)     → todo, conservando expansión (Refresh / foco)
 *   - expandir una carpeta  → carga si no está en caché
 * Nunca por (des)montaje. Mismo patrón que WebSocketService / useVoiceLive:
 * singleton de módulo + suscripción, sin hooks dentro del store.
 */
import { useSyncExternalStore } from 'react';
import { apiClient } from '../../api/apiClient';

export interface DirEntry {
  name: string;
  type: 'directory' | 'file';
  size?: number | null;
  status?: string | null; // "M" | "?" | null
}

export type Level = DirEntry[] | 'loading';

interface WsState {
  levels: Record<string, Level>; // key: dir relativo ('' = raíz)
  expanded: Set<string>;
}

const EMPTY: WsState = { levels: {}, expanded: new Set() };
const states = new Map<string, WsState>();
const listeners = new Set<() => void>();

function get(ws: string): WsState {
  return states.get(ws) ?? EMPTY;
}

function set(ws: string, next: WsState): void {
  states.set(ws, next);
  listeners.forEach((l) => l());
}

function parentOf(path: string): string {
  const i = path.lastIndexOf('/');
  return i < 0 ? '' : path.slice(0, i);
}

async function load(ws: string, dir: string): Promise<void> {
  const cur = get(ws);
  set(ws, { ...cur, levels: { ...cur.levels, [dir]: 'loading' } });
  let entries: DirEntry[] = [];
  try {
    const r: { entries?: DirEntry[] } = await apiClient.get(
      `/v4/workspace/${encodeURIComponent(ws)}/entries`,
      { params: dir ? { path: dir } : undefined }
    );
    entries = r.entries ?? [];
  } catch {
    entries = [];
  }
  const now = get(ws);
  set(ws, { ...now, levels: { ...now.levels, [dir]: entries } });
}

export const workspaceTree = {
  subscribe(l: () => void): () => void {
    listeners.add(l);
    return () => listeners.delete(l);
  },
  getSnapshot(ws: string): WsState {
    return get(ws);
  },

  /** Garantiza que un nivel esté cargado (no recarga si ya está en caché). */
  ensure(ws: string, dir = ''): void {
    if (get(ws).levels[dir] === undefined) void load(ws, dir);
  },

  toggle(ws: string, dir: string): void {
    const cur = get(ws);
    const expanded = new Set(cur.expanded);
    if (expanded.has(dir)) expanded.delete(dir);
    else expanded.add(dir);
    set(ws, { ...cur, expanded });
    if (!cur.expanded.has(dir)) workspaceTree.ensure(ws, dir);
  },

  /**
   * Invalida el nivel que contiene `path` (o `path` mismo si es un dir
   * cargado). Para create/delete/rename/save originados por MACAE: el
   * cliente conoce el path, así que releer UN nivel basta y las marcas git
   * (M/?) del nivel y sus ancestros vuelven correctas.
   */
  invalidate(ws: string, path: string): void {
    const cur = get(ws);
    const dirs = new Set<string>([parentOf(path)]);
    if (cur.levels[path] !== undefined) dirs.add(path);
    // Ancestros cargados: sus marcas agregadas (M/?) también cambian.
    let p = parentOf(path);
    while (p) {
      p = parentOf(p);
      dirs.add(p);
    }
    dirs.forEach((d) => {
      if (cur.levels[d] !== undefined) void load(ws, d);
    });
  },

  /**
   * Reconciliación completa contra el filesystem real: relee la raíz y TODOS
   * los niveles actualmente expandidos, conservando la expansión para no
   * perderle la posición al usuario. Es lo que hace el botón Refresh y lo
   * que conviene disparar al recuperar visibilidad.
   */
  invalidateAll(ws: string): void {
    const cur = get(ws);
    const dirs = new Set<string>(['']);
    cur.expanded.forEach((d) => dirs.add(d));
    dirs.forEach((d) => void load(ws, d));
  },

  /** Cambio de workspace: descarta el estado de ese ws (raro; explícito). */
  reset(ws: string): void {
    states.delete(ws);
    listeners.forEach((l) => l());
  },
};

/** Vista React del store para un workspace. */
export function useWorkspaceTree(ws: string): WsState {
  return useSyncExternalStore(
    workspaceTree.subscribe,
    () => workspaceTree.getSnapshot(ws),
    () => workspaceTree.getSnapshot(ws)
  );
}
