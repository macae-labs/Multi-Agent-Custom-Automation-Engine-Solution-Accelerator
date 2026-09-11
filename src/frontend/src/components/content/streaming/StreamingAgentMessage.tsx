import React, { useContext } from 'react';
import { AgentMessageData, AgentMessageType } from '@/models';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypePrism from 'rehype-prism';
import { Body1, Tag, makeStyles, tokens } from '@fluentui/react-components';
import { TaskService } from '@/services';
import { HtmlCodeToggle } from '../HtmlPreview';
import {
  ArrowDownloadRegular,
  CopyRegular,
  PersonRegular,
  PlayRegular,
  ShareRegular,
} from '@fluentui/react-icons';
import { getAgentIcon, getAgentDisplayName } from '@/utils/agentIconUtils';
import { resolveApiUrl } from '@/api/config';

interface StreamingAgentMessageProps {
  agentMessages: AgentMessageData[];
  planData?: any;
  planApprovalRequest?: any;
}

const useStyles = makeStyles({
  container: {
    maxWidth: '800px',
    margin: '0 auto 32px auto',
    padding: '0 24px',
    display: 'flex',
    alignItems: 'flex-start',
    gap: '16px',
    fontFamily: tokens.fontFamilyBase,
  },
  avatar: {
    width: '32px',
    height: '32px',
    borderRadius: '50%',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    flexShrink: 0,
  },
  humanAvatar: {
    backgroundColor: 'var(--colorBrandBackground)',
  },
  botAvatar: {
    backgroundColor: 'var(--colorNeutralBackground3)',
  },
  messageContent: {
    flex: 1,
    maxWidth: 'calc(100% - 48px)',
    display: 'flex',
    flexDirection: 'column',
  },
  humanMessageContent: {
    alignItems: 'flex-end',
  },
  botMessageContent: {
    alignItems: 'flex-start',
  },
  agentHeader: {
    display: 'flex',
    alignItems: 'center',
    gap: '12px',
    marginBottom: '8px',
  },
  agentName: {
    fontWeight: '600',
    fontSize: '14px',
    color: 'var(--colorNeutralForeground1)',
    lineHeight: '20px',
  },
  messageBubble: {
    padding: '12px 16px',
    borderRadius: '8px',
    fontSize: '14px',
    lineHeight: '1.5',
    wordWrap: 'break-word',
    // Padding must count INSIDE maxWidth: 100% — content-box made the bubble
    // 736px in a 704px parent, giving the whole chat a horizontal scrollbar.
    boxSizing: 'border-box',
  },
  humanBubble: {
    backgroundColor: 'var(--colorBrandBackground)',
    color: 'white !important', // Force white text in both light and dark modes
    maxWidth: '80%',
    padding: '12px 16px',
    lineHeight: '1.5',
    alignSelf: 'flex-end',
  },
  botBubble: {
    backgroundColor: 'var(--colorNeutralBackground2)',
    color: 'var(--colorNeutralForeground1)',
    maxWidth: '100%',
    alignSelf: 'flex-start',
  },

  clarificationBubble: {
    backgroundColor: 'var(--colorNeutralBackground2)',
    color: 'var(--colorNeutralForeground1)',
    padding: '6px 8px',
    borderRadius: '8px',
    fontSize: '14px',
    lineHeight: '1.5',
    wordWrap: 'break-word',
    maxWidth: '100%',
    alignSelf: 'flex-start',
  },

  actionContainer: {
    display: 'flex',
    alignItems: 'center',
    marginTop: '12px',
    paddingTop: '8px',
    borderTop: '1px solid var(--colorNeutralStroke2)',
  },

  copyButton: {
    height: '28px',
    width: '28px',
  },
  sampleTag: {
    fontSize: '11px',
    opacity: 0.7,
  },
  // Botón del overlay de imagen (patrón Gemini): base transparente — el
  // feedback es el hover-state animado, no un chip sólido. :hover no existe
  // en estilos inline; por eso vive aquí.
  imageOverlayButton: {
    width: '32px',
    height: '32px',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    border: 'none',
    borderRadius: '8px',
    backgroundColor: 'rgba(0, 0, 0, 0)',
    color: '#fff',
    cursor: 'pointer',
    padding: '0',
    // Legibilidad del icono blanco sobre zonas claras de la imagen.
    filter: 'drop-shadow(0 1px 2px rgba(0, 0, 0, 0.6))',
    transitionProperty: 'background-color, transform',
    transitionDuration: '120ms',
    ':hover': {
      backgroundColor: 'rgba(0, 0, 0, 0.35)',
    },
    ':active': {
      transform: 'scale(0.92)',
    },
  },
  // Adjuntos multimedia del mensaje (video hoy; generated_file por media_type
  // después). Viven FUERA de la burbuja y del flujo del markdown: el modelo
  // suele escribir la URL dentro de una viñeta, y un reproductor incrustado
  // ahí hereda sangría de lista, padding de burbuja y columna del avatar
  // (en móvil quedaba en 222 px de 390). Aquí ocupa la columna completa.
  mediaBlock: {
    alignSelf: 'stretch',
    display: 'grid',
    rowGap: '8px',
    marginTop: '8px',
    '@media (max-width: 640px)': {
      // En pantallas estrechas el medio se extiende sobre el canal del avatar
      // (32px + gap 16px): el avatar solo ocupa la primera línea del mensaje.
      marginLeft: '-48px',
      width: 'calc(100% + 48px)',
    },
  },
  // Chip inline que sustituye al enlace del video dentro de la prosa: el
  // artefacto se ve abajo, el texto conserva su lugar.
  videoChip: {
    display: 'inline-flex',
    alignItems: 'center',
    columnGap: '6px',
    padding: '2px 10px 2px 8px',
    borderRadius: '999px',
    backgroundColor: 'var(--colorNeutralBackground3)',
    color: 'var(--colorNeutralBrandForeground1)',
    fontSize: '13px',
    lineHeight: '20px',
    textDecoration: 'none',
    verticalAlign: 'middle',
    maxWidth: '100%',
    ':hover': {
      backgroundColor: 'var(--colorNeutralBackground3Hover)',
      textDecoration: 'none',
    },
  },
});

// Check if message is a clarification request
const isClarificationMessage = (content: string): boolean => {
  const clarificationKeywords = [
    'need clarification',
    'please clarify',
    'could you provide more details',
    'i need more information',
    'please specify',
    'what do you mean by',
    'clarification about',
  ];

  const lowerContent = content.toLowerCase();
  return clarificationKeywords.some((keyword) =>
    lowerContent.includes(keyword)
  );
};

// Acciones SOBRE la imagen (patrón Gemini): share / copy / download viven en
// un overlay al hover de la imagen generada — la imagen es el artefacto y sus
// acciones van con ella, no en una ristra de chips aparte. El click en la
// imagen sigue abriendo el archivo completo.
//
// Los botones están SIEMPRE en el DOM (como en Gemini) y se ocultan/revelan
// por CSS: montarlos solo en hover los hacía in-inspeccionables (mouseleave
// los desmontaba antes de que el picker de DevTools llegara) y no animables.
const GeneratedImage = ({ alt, src, ...props }: any) => {
  const styles = useStyles();
  const url = resolveApiUrl(src);
  const filename = alt || 'generated.png';
  const [hover, setHover] = React.useState(false);

  const download = async (e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      const blob = await (await fetch(url)).blob();
      const objectUrl = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = objectUrl;
      a.download = filename;
      a.click();
      setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
    } catch {
      // ignore (network/permission errors)
    }
  };
  const copy = async (e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      const blob = await (await fetch(url)).blob();
      await navigator.clipboard.write([
        new ClipboardItem({ [blob.type]: blob }),
      ]);
    } catch {
      // Safari no permite ClipboardItem con fetch async / permiso denegado:
      // al menos queda el enlace en el portapapeles.
      await navigator.clipboard.writeText(url);
    }
  };
  const share = async (e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      if (navigator.share) {
        await navigator.share({ title: filename, url });
      } else {
        await navigator.clipboard.writeText(url);
      }
    } catch {
      // usuario cerró el share sheet — no es un error
    }
  };

  return (
    // span (no div): el markdown envuelve la imagen en un <p>; un div ahí es
    // HTML inválido y React lo advierte. display:block conserva el layout
    // exacto que tenía la imagen sola.
    <span
      style={{ position: 'relative', display: 'block', margin: '8px 0' }}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      onFocus={() => setHover(true)}
      onBlur={() => setHover(false)}
    >
      <img
        alt={alt ?? ''}
        src={url}
        {...props}
        style={{
          // Rectangular card, visually paired with the chat input: full column
          // width, 12px radius, 3:2 (the generator now produces 1536×1024 —
          // the same ratio — so the image fills the card with nothing to crop).
          width: '100%',
          aspectRatio: '3 / 2',
          objectFit: 'cover',
          maxHeight: '480px',
          display: 'block',
          borderRadius: '12px',
          cursor: 'zoom-in',
        }}
        onClick={() => window.open(url, '_blank', 'noopener')}
      />
      <span
        style={{
          position: 'absolute',
          top: '8px',
          right: '8px',
          display: 'flex',
          gap: '4px',
          // Siempre en DOM; visibilidad animada por CSS (fade + deslizamiento).
          opacity: hover ? 1 : 0,
          visibility: hover ? 'visible' : 'hidden',
          transform: hover ? 'translateY(0)' : 'translateY(-4px)',
          pointerEvents: hover ? 'auto' : 'none',
          transition: 'opacity 150ms ease, transform 150ms ease',
        }}
      >
        <button
          type="button"
          title="Compartir"
          aria-label="Compartir imagen"
          className={styles.imageOverlayButton}
          onClick={share}
        >
          <ShareRegular fontSize={16} />
        </button>
        <button
          type="button"
          title="Copiar imagen"
          aria-label="Copiar imagen"
          className={styles.imageOverlayButton}
          onClick={copy}
        >
          <CopyRegular fontSize={16} />
        </button>
        <button
          type="button"
          title="Descargar"
          aria-label="Descargar imagen"
          className={styles.imageOverlayButton}
          onClick={download}
        >
          <ArrowDownloadRegular fontSize={16} />
        </button>
      </span>
    </span>
  );
};

// Static markdown renderers — hoisted to module scope so the `components`
// object is stable across renders. An inline object here would be a new
// reference on every render and defeat ReactMarkdown's internal memoization.
// Shared by every agent-bubble ReactMarkdown (this file + StreamingBufferMessage)
// so links and generated images render identically everywhere.

// URL de video (Higgsfield, Sora, blobs propios…). Se mira el path sin query:
// los CDNs firman con `?token=` y la extensión queda antes.
const VIDEO_URL_RE = /\.(mp4|webm|mov|m4v)(?=($|[?#]))/i;
export const isVideoUrl = (u?: string) =>
  !!u && VIDEO_URL_RE.test(u.split(/[?#]/)[0]);

// Nombre para "Descargar": el alt/label del markdown solo sirve si ya es un
// nombre de archivo de video; si es un rótulo ("Ver video") o la URL misma
// (autolink de remark-gfm), se toma el basename del path de la URL.
export const videoFilename = (alt: string | undefined, url: string) => {
  if (alt && VIDEO_URL_RE.test(alt) && !/^https?:/i.test(alt)) return alt;
  const base = url.split(/[?#]/)[0].split('/').pop() ?? '';
  return VIDEO_URL_RE.test(base) ? base : 'generated.mp4';
};

// Único hijo de texto de un nodo hast (label de `[label](url)` o la URL de un
// autolink). react-markdown entrega `children` como string cuando hay UN hijo,
// así que no se puede indexar `children[0]` (sería la primera letra).
const soleTextChild = (node: any): string | null => {
  const kids = node?.children;
  if (!Array.isArray(kids) || kids.length !== 1 || kids[0]?.type !== 'text') {
    return null;
  }
  return String(kids[0].value ?? '');
};

// Video generado: mismo contrato de acciones que GeneratedImage (overlay
// compartir/descargar al hover; no hay "copiar", el portapapeles no admite
// video/*). Se renderiza en el bloque de adjuntos del mensaje (MessageMedia),
// no en el sitio del enlace. La proporción es la INTRÍNSECA del archivo
// (16:9 solo como placeholder hasta loadedmetadata): un vertical 9:16 no se
// encajona en negro dentro de un marco 16:9.
const GeneratedVideo = ({ alt, src }: { alt?: string; src: string }) => {
  const styles = useStyles();
  const url = resolveApiUrl(src);
  const filename = videoFilename(alt, url);
  const [hover, setHover] = React.useState(false);
  const [ratio, setRatio] = React.useState(16 / 9);
  const portrait = ratio < 1;

  const download = async (e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      const blob = await (await fetch(url)).blob();
      const objectUrl = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = objectUrl;
      a.download = filename;
      a.click();
      setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
    } catch {
      // CORS del CDN puede impedir el fetch: abrir en pestaña nueva como fallback
      window.open(url, '_blank', 'noopener');
    }
  };
  const share = async (e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      if (navigator.share) await navigator.share({ title: filename, url });
      else await navigator.clipboard.writeText(url);
    } catch {
      // usuario cerró el share sheet
    }
  };

  return (
    <span
      style={{
        position: 'relative',
        display: 'block',
        // Horizontal: llena la columna. Vertical: alto acotado y ancho al
        // contenido, así el overlay queda sobre el video y no sobre un hueco.
        width: portrait ? 'fit-content' : '100%',
        maxWidth: '100%',
      }}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      onFocus={() => setHover(true)}
      onBlur={() => setHover(false)}
    >
      {/* playsInline: iOS reproduce dentro de la card, no a pantalla completa */}
      <video
        src={url}
        controls
        playsInline
        preload="metadata"
        aria-label={alt ?? 'video generado'}
        onLoadedMetadata={(e) => {
          const v = e.currentTarget;
          if (v.videoWidth && v.videoHeight) {
            setRatio(v.videoWidth / v.videoHeight);
          }
        }}
        style={{
          aspectRatio: String(ratio),
          ...(portrait
            ? { height: 'min(480px, 70vh)', width: 'auto', maxWidth: '100%' }
            : { width: '100%', maxHeight: 'min(480px, 70vh)' }),
          display: 'block',
          borderRadius: '12px',
          backgroundColor: '#000',
        }}
      />
      <span
        style={{
          position: 'absolute',
          top: '8px',
          right: '8px',
          display: 'flex',
          gap: '4px',
          opacity: hover ? 1 : 0,
          visibility: hover ? 'visible' : 'hidden',
          transform: hover ? 'translateY(0)' : 'translateY(-4px)',
          pointerEvents: hover ? 'auto' : 'none',
          transition: 'opacity 150ms ease, transform 150ms ease',
        }}
      >
        <button
          type="button"
          title="Compartir"
          aria-label="Compartir video"
          className={styles.imageOverlayButton}
          onClick={share}
        >
          <ShareRegular fontSize={16} />
        </button>
        <button
          type="button"
          title="Descargar"
          aria-label="Descargar video"
          className={styles.imageOverlayButton}
          onClick={download}
        >
          <ArrowDownloadRegular fontSize={16} />
        </button>
      </span>
    </span>
  );
};

// Chip inline que ocupa el lugar del enlace al video dentro de la prosa.
const VideoChip = ({ href, label }: { href: string; label?: string }) => {
  const styles = useStyles();
  const url = resolveApiUrl(href);
  const text =
    label && !/^https?:/i.test(label) ? label : videoFilename(undefined, url);
  return (
    <a
      className={styles.videoChip}
      href={url}
      target="_blank"
      rel="noopener noreferrer"
      title={url}
    >
      <PlayRegular fontSize={14} />
      <span
        style={{
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
        }}
      >
        {text}
      </span>
    </a>
  );
};

export interface VideoLink {
  url: string;
  label?: string;
}

// Videos referenciados en el markdown de UN mensaje, en orden y sin repetir:
// `[label](url.mp4)`, `![alt](url.mp4)` y URLs sueltas. Determinista sobre la
// fuente, así el bloque de adjuntos no depende de cómo react-markdown parte
// los nodos ni de en qué lista o párrafo cayó la URL.
const MD_LINK_RE = /!?\[([^\]]*)\]\(\s*<?([^\s)>]+)>?(?:\s+"[^"]*")?\s*\)/g;
const BARE_URL_RE = /https?:\/\/[^\s<>()[\]]+/g;
export const extractVideoLinks = (content: string): VideoLink[] => {
  const out: VideoLink[] = [];
  const seen = new Set<string>();
  const add = (url: string, label?: string) => {
    if (!isVideoUrl(url) || seen.has(url)) return;
    seen.add(url);
    const clean = label?.trim();
    out.push(clean ? { url, label: clean } : { url });
  };
  let m: RegExpExecArray | null;
  MD_LINK_RE.lastIndex = 0;
  while ((m = MD_LINK_RE.exec(content)) !== null) add(m[2], m[1]);
  BARE_URL_RE.lastIndex = 0;
  while ((m = BARE_URL_RE.exec(content)) !== null) {
    add(m[0].replace(/[.,;:!?]+$/, ''));
  }
  return out;
};

// Bloque de adjuntos multimedia del mensaje: debajo de la burbuja, a ancho de
// columna. Hoy lo alimenta extractVideoLinks; el canal generated_file con
// media_type se conecta aquí mismo cuando llegue.
const MessageMedia = ({ videos }: { videos: VideoLink[] }) => {
  const styles = useStyles();
  if (!videos.length) return null;
  return (
    <div className={styles.mediaBlock} data-testid="message-media">
      {videos.map((v) => (
        <GeneratedVideo key={v.url} alt={v.label} src={v.url} />
      ))}
    </div>
  );
};

export const markdownComponents = {
  // Wide code blocks scroll inside their own box; without this a long
  // unbreakable line widens the whole message column (horizontal scrollbar
  // on the chat itself).
  pre: ({ node, ...props }: any) => (
    <pre
      {...props}
      style={{
        maxWidth: '100%',
        boxSizing: 'border-box',
        overflowX: 'auto',
        borderRadius: '8px',
      }}
    />
  ),
  // A NAMED fence (or any ```html fence) is a generated FILE: it goes to the
  // artifact panel as a chip instead of dumping the whole code into the
  // message. Unnamed non-html fences stay inline as prose snippets.
  code: ({ node, className, children, ...props }: any) => {
    const isBlock = !props.inline;
    const lang = (className || '').replace('language-', '');
    // eslint-disable-next-line react-hooks/rules-of-hooks
    const fenceNames = useContext(FenceNamesContext);
    const startLine = node?.position?.start?.line as number | undefined;
    const filename =
      isBlock && startLine != null ? fenceNames?.get(startLine) : undefined;
    if (isBlock && (lang === 'html' || filename)) {
      // The raw source must come from the hast node, NOT String(children):
      // rehype-prism has already replaced children with highlight <span>
      // elements, and String() over React elements yields "[object Object]".
      const hastText = (n: any): string =>
        n?.type === 'text'
          ? n.value || ''
          : ((n?.children as any[]) || []).map(hastText).join('');
      const raw = hastText(node).replace(/\n$/, '');
      const codeBlock = (
        <pre
          style={{
            maxWidth: '100%',
            boxSizing: 'border-box',
            overflowX: 'auto',
            borderRadius: '8px',
          }}
        >
          <code className={className} {...props}>
            {children}
          </code>
        </pre>
      );
      return (
        <HtmlCodeToggle
          code={raw}
          codeBlock={codeBlock}
          filename={filename}
          lang={lang || 'txt'}
        />
      );
    }
    return (
      <code className={className} {...props}>
        {children}
      </code>
    );
  },
  img: ({ node, ...props }: any) =>
    isVideoUrl(props.src) ? (
      <VideoChip href={props.src} label={props.alt} />
    ) : (
      <GeneratedImage {...props} />
    ),
  a: ({ node, children, href, ...props }: any) => {
    // Un enlace a un video NO se incrusta aquí: dentro de una viñeta o un
    // párrafo heredaría sangría y paddings. Queda un chip y el reproductor va
    // al bloque de adjuntos del mensaje (MessageMedia). Aplica a
    // `[label](url.mp4)` y a la URL suelta (autolink): en ambos el <a> tiene
    // un único hijo de texto. Un enlace con contenido compuesto sigue igual.
    const label = soleTextChild(node);
    if (isVideoUrl(href) && label !== null) {
      return <VideoChip href={href} label={label} />;
    }
    return (
      <a
        href={resolveApiUrl(href)}
        {...props}
        style={{
          color: 'var(--colorNeutralBrandForeground1)',
          textDecoration: 'none',
        }}
        onMouseEnter={(e) => {
          e.currentTarget.style.textDecoration = 'underline';
        }}
        onMouseLeave={(e) => {
          e.currentTarget.style.textDecoration = 'none';
        }}
      >
        {children}
      </a>
    );
  },
};

// Filename map for the fences of ONE message: fence start line → the filename
// the model wrote just above it (heading/bold "templates/index.html" style).
// The code renderer reads its own start line from the hast node and looks the
// name up here — that name IS the artifact identity for the panel.
const FenceNamesContext = React.createContext<Map<number, string> | null>(null);

const FILENAME_RE = /([\w@-][\w@./-]*\.[A-Za-z0-9]{1,8})/;

function extractFenceNames(md: string): Map<number, string> {
  const names = new Map<number, string>();
  const lines = md.split('\n');
  for (let i = 0; i < lines.length; i++) {
    if (!/^\s*(```|~~~)\S*/.test(lines[i])) continue;
    // Look upward past blank lines for a short "title" line with a filename.
    for (let j = i - 1, seen = 0; j >= 0 && seen < 3; j--) {
      const line = lines[j].trim();
      if (!line) continue;
      seen++;
      if (line.length > 120) break; // prose paragraph, not a file title
      const m = line.match(FILENAME_RE);
      if (m) {
        names.set(i + 1, m[1]); // remark positions are 1-based
        break;
      }
      break; // only the nearest non-blank line counts as the title
    }
  }
  return names;
}

// Isolated, memoized Markdown. Re-parses (and re-highlights) ONLY when its
// content string changes. This is the key fix: appending a streaming token to
// the last message no longer re-parses the Markdown of every previous message.
const AgentMarkdown = React.memo(({ content }: { content: string }) => {
  const fenceNames = React.useMemo(() => extractFenceNames(content), [content]);
  return (
    <FenceNamesContext.Provider value={fenceNames}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypePrism]}
        components={markdownComponents}
      >
        {content}
      </ReactMarkdown>
    </FenceNamesContext.Provider>
  );
});
AgentMarkdown.displayName = 'AgentMarkdown';

interface AgentMessageItemProps {
  msg: AgentMessageData;
  planData?: any;
  planApprovalRequest?: any;
}

// A single chat row, memoized. Even if it re-renders because planData/approval
// references churn, the expensive Markdown inside stays protected by
// AgentMarkdown's content-based memoization.
export const AgentMessageItem = React.memo(
  ({ msg, planData, planApprovalRequest }: AgentMessageItemProps) => {
    const styles = useStyles();
    const isHuman = msg.agent_type === AgentMessageType.HUMAN_AGENT;
    const isClarification =
      !isHuman && isClarificationMessage(msg.content || '');
    const content = TaskService.cleanHRAgent(msg.content) || '';
    // Adjuntos del mensaje (solo respuestas del agente): se extraen de la
    // fuente markdown una vez por contenido, como AgentMarkdown.
    const videos = React.useMemo(
      () => (isHuman ? [] : extractVideoLinks(content)),
      [content, isHuman]
    );

    return (
      <div
        className={styles.container}
        style={{ flexDirection: isHuman ? 'row-reverse' : 'row' }}
      >
        {/* Avatar */}
        <div
          className={`${styles.avatar} ${isHuman ? styles.humanAvatar : styles.botAvatar}`}
        >
          {isHuman ? (
            <PersonRegular style={{ fontSize: '16px', color: 'white' }} />
          ) : (
            getAgentIcon(msg.agent, planData, planApprovalRequest)
          )}
        </div>

        {/* Message Content */}
        <div
          className={`${styles.messageContent} ${isHuman ? styles.humanMessageContent : styles.botMessageContent}`}
        >
          {/* Agent Header (only for bots) */}
          {!isHuman && (
            <div className={styles.agentHeader}>
              <Body1 className={styles.agentName}>
                {getAgentDisplayName(msg.agent)}
              </Body1>
              <Tag appearance="brand">AI Agent</Tag>
            </div>
          )}

          {/* Message Bubble */}
          <div
            className={
              isHuman
                ? `${styles.messageBubble} ${styles.humanBubble}`
                : isClarification
                  ? styles.clarificationBubble
                  : `${styles.messageBubble} ${styles.botBubble}`
            }
          >
            <AgentMarkdown content={content} />
          </div>
          {!isHuman && <MessageMedia videos={videos} />}
        </div>
      </div>
    );
  }
);
AgentMessageItem.displayName = 'AgentMessageItem';

const RenderAgentMessages: React.FC<StreamingAgentMessageProps> = ({
  agentMessages,
  planData,
  planApprovalRequest,
}) => {
  if (!agentMessages?.length) return null;

  // Filter out messages with empty content
  const validMessages = agentMessages.filter((msg) => msg.content?.trim());
  if (!validMessages.length) return null;

  return (
    <>
      {validMessages.map((msg, index) => (
        <AgentMessageItem
          key={`${msg.agent}-${msg.timestamp}-${index}`}
          msg={msg}
          planData={planData}
          planApprovalRequest={planApprovalRequest}
        />
      ))}
    </>
  );
};

export default RenderAgentMessages;
