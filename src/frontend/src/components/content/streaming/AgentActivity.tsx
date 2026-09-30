/**
 * AgentActivity — actividad del agente como ESTADO, no como texto.
 *
 * Dos modos sobre la misma fuente de datos (`tool_activity` en vivo o
 * `metadata.turn_log` persistido):
 *  - <AgentActivityIndicator/>: mientras el turno está vivo. Dot-matrix
 *    loader + texto de estado con shimmer, derivado del último evento.
 *  - <TurnDeedsLog/>: al cerrar el turno / al recargar. Lista compacta y
 *    plegable de las tools ejecutadas, fuera de la burbuja del modelo.
 */
import React from 'react';
import { makeStyles, tokens } from '@fluentui/react-components';
import { useAppSelector } from '@/store/hooks';
import {
  selectToolActivities,
  type ToolActivityEvent,
  type TurnDeed,
} from '@/store/slices/streamingSlice';

const useStyles = makeStyles({
  row: {
    maxWidth: '800px',
    margin: '0 auto 32px auto',
    padding: '0 24px',
    display: 'flex',
    alignItems: 'center',
    gap: '12px',
    color: tokens.colorNeutralForeground2,
    fontSize: '14px',
    fontFamily: tokens.fontFamilyBase,
  },
  matrix: {
    display: 'grid',
    gridTemplateColumns: 'repeat(3, 4px)',
    gap: '3px',
    flexShrink: 0,
  },
  dot: {
    width: '4px',
    height: '4px',
    borderRadius: '1px',
    backgroundColor: tokens.colorBrandForeground1,
    animationName: {
      '0%, 100%': { opacity: 0.15 },
      '50%': { opacity: 1 },
    },
    animationDuration: '1.2s',
    animationIterationCount: 'infinite',
    animationTimingFunction: 'ease-in-out',
  },
  shimmer: {
    backgroundImage: `linear-gradient(90deg, ${tokens.colorNeutralForeground3} 0%, ${tokens.colorNeutralForeground1} 50%, ${tokens.colorNeutralForeground3} 100%)`,
    backgroundSize: '200% 100%',
    backgroundClip: 'text',
    WebkitBackgroundClip: 'text',
    color: 'transparent',
    animationName: {
      '0%': { backgroundPosition: '200% 0' },
      '100%': { backgroundPosition: '-200% 0' },
    },
    animationDuration: '2s',
    animationIterationCount: 'infinite',
    animationTimingFunction: 'linear',
    whiteSpace: 'nowrap',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
  },
  log: {
    fontSize: '12px',
    color: tokens.colorNeutralForeground3,
    marginBottom: '6px',
    alignSelf: 'stretch',
  },
  summary: {
    cursor: 'pointer',
    userSelect: 'none',
    listStyle: 'none',
    '::-webkit-details-marker': { display: 'none' },
  },
  deed: {
    display: 'flex',
    gap: '6px',
    alignItems: 'baseline',
    padding: '2px 0 2px 12px',
    fontFamily: tokens.fontFamilyMonospace,
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
  },
  ok: { color: tokens.colorPaletteGreenForeground1 },
  err: { color: tokens.colorPaletteRedForeground1 },
});

// Un solo criterio de etiqueta para vivo y persistido.
const toolLabel = (tool: string, server?: string) =>
  server && server !== 'workspace' ? `${tool} · ${server}` : tool;

const statusText = (last: ToolActivityEvent | undefined, fallback: string) => {
  if (!last) return fallback;
  if (last.activity === 'thinking' && last.detail) return last.detail;
  if (last.activity === 'calling') return toolLabel(last.tool, last.server);
  if (last.activity === 'result')
    return `${toolLabel(last.tool, last.server)} ${last.success === false ? '✕' : '✓'}`;
  return fallback;
};

export const AgentActivityIndicator: React.FC<{
  fallback?: string;
}> = ({ fallback = 'Thinking…' }) => {
  const styles = useStyles();
  const activities = useAppSelector(selectToolActivities);
  const last = activities[activities.length - 1];
  return (
    <div className={styles.row} role="status" aria-live="polite">
      <div className={styles.matrix} aria-hidden>
        {Array.from({ length: 9 }, (_, i) => (
          <span
            key={i}
            className={styles.dot}
            style={{
              animationDelay: `${(i % 3) * 0.12 + Math.floor(i / 3) * 0.2}s`,
            }}
          />
        ))}
      </div>
      <span className={styles.shimmer}>{statusText(last, fallback)}</span>
    </div>
  );
};

/** Registro estático del turno. Acepta deeds persistidos o eventos en vivo. */
export const TurnDeedsLog: React.FC<{
  deeds?: TurnDeed[];
}> = ({ deeds }) => {
  const styles = useStyles();
  if (!deeds?.length) return null;
  return (
    <details className={styles.log}>
      <summary className={styles.summary}>
        {deeds.length} tool call{deeds.length === 1 ? '' : 's'}
      </summary>
      {deeds.map((d, i) => (
        <div key={i} className={styles.deed} title={d.args?.text}>
          <span className={d.status === 'error' ? styles.err : styles.ok}>
            {d.status === 'error' ? '✕' : '✓'}
          </span>
          <span>{toolLabel(d.tool, d.server)}</span>
          {d.args?.text ? <span>{d.args.text}</span> : null}
        </div>
      ))}
    </details>
  );
};

/** Convierte los eventos en vivo del turno a deeds (misma forma que el backend). */
export const deedsFromActivities = (
  events: ToolActivityEvent[]
): TurnDeed[] => {
  const out: TurnDeed[] = [];
  const pending = new Map<string, string>();
  for (const e of events) {
    const key = toolLabel(e.tool, e.server);
    if (e.activity === 'calling') pending.set(key, e.args ?? '');
    else if (e.activity === 'result') {
      const args = pending.get(key) ?? '';
      pending.delete(key);
      out.push({
        server: e.server ?? 'workspace',
        tool: e.tool,
        status: e.success === false ? 'error' : 'success',
        args: { text: args, chars: args.length, truncated: false },
        result: {
          text: e.result_preview ?? '',
          chars: (e.result_preview ?? '').length,
          truncated: false,
        },
      });
    }
  }
  return out;
};
