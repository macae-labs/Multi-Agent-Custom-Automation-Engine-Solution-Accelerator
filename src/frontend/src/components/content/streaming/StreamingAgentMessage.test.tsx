import { render, screen, within } from '@testing-library/react';
import { FluentProvider, teamsLightTheme } from '@fluentui/react-components';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  AgentMessageItem,
  extractVideoLinks,
  isVideoUrl,
  markdownComponents,
  videoFilename,
} from './StreamingAgentMessage';
import { AgentMessageType } from '@/models';

// Renderiza exactamente como las burbujas del agente (mismos plugins y el
// mismo objeto `components` compartido con StreamingBufferMessage).
const md = (text: string) =>
  render(
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
      {text}
    </ReactMarkdown>
  );

const MP4 = 'https://cdn.example.com/gen/512fda58/output.mp4';
const SIGNED = `${MP4}?token=abc&exp=123`;

// Forma real del mensaje final del modelo para el job 512fda58: lista con
// rótulos en negrita y la URL suelta dentro de una viñeta.
const REAL = [
  'Resultado real de Higgsfield:',
  '',
  '- **Job ID:** `512fda58`',
  '- **Estado:** completed ✅',
  `- **URL del video:** ${MP4}`,
  '',
  'Generado con `seedance_2_0`.',
].join('\n');

const agentMsg = (content: string, human = false) => ({
  agent: 'MacaeMcpServer',
  agent_type: (human
    ? AgentMessageType.HUMAN_AGENT
    : 'ai_agent') as AgentMessageType,
  timestamp: 1,
  steps: [],
  next_steps: [],
  content,
  raw_data: '',
});

const item = (content: string, human = false) =>
  render(
    <FluentProvider theme={teamsLightTheme}>
      <AgentMessageItem msg={agentMsg(content, human)} />
    </FluentProvider>
  );

describe('isVideoUrl', () => {
  it('reconoce extensiones de video antes de query/fragment', () => {
    expect(isVideoUrl(MP4)).toBe(true);
    expect(isVideoUrl(SIGNED)).toBe(true);
    expect(isVideoUrl('https://x/y.webm#t=2')).toBe(true);
  });
  it('rechaza imágenes, páginas y vacíos', () => {
    expect(isVideoUrl('https://x/y.png')).toBe(false);
    expect(isVideoUrl('https://x/watch?v=mp4')).toBe(false);
    expect(isVideoUrl(undefined)).toBe(false);
  });
});

describe('videoFilename', () => {
  it('usa el alt solo si ya es un nombre de archivo de video', () => {
    expect(videoFilename('coche.mp4', MP4)).toBe('coche.mp4');
  });
  it('cae al basename de la URL para rótulos o URLs como alt', () => {
    expect(videoFilename('Ver video', SIGNED)).toBe('output.mp4');
    expect(videoFilename(MP4, MP4)).toBe('output.mp4');
  });
  it('fallback fijo cuando el path no trae nombre', () => {
    expect(videoFilename(undefined, 'https://x/stream.mp4/')).toBe(
      'generated.mp4'
    );
  });
});

describe('extractVideoLinks', () => {
  it('encuentra la URL suelta dentro de una viñeta (forma real)', () => {
    expect(extractVideoLinks(REAL)).toEqual([{ url: MP4 }]);
  });
  it('toma el rótulo de [label](url) y el alt de ![alt](url)', () => {
    expect(extractVideoLinks(`[Ver video](${SIGNED})`)).toEqual([
      { url: SIGNED, label: 'Ver video' },
    ]);
    expect(extractVideoLinks(`![clip.mp4](${MP4})`)).toEqual([
      { url: MP4, label: 'clip.mp4' },
    ]);
  });
  it('no repite la misma URL aunque aparezca dos veces', () => {
    const twice = `[Ver video](${MP4})\n\nTambién: ${MP4}`;
    expect(extractVideoLinks(twice)).toHaveLength(1);
  });
  it('ignora imágenes y páginas, y limpia puntuación final', () => {
    expect(extractVideoLinks('![g](https://x/g.png) https://x/page')).toEqual(
      []
    );
    expect(extractVideoLinks(`Listo: ${MP4}.`)).toEqual([{ url: MP4 }]);
  });
});

describe('markdownComponents · video en la prosa = chip, no reproductor', () => {
  it('[label](url.mp4) es un chip enlazado al archivo', () => {
    md(`[Ver video](${SIGNED})`);
    const chip = screen.getByRole('link', { name: 'Ver video' });
    expect(chip.getAttribute('href')).toBe(SIGNED);
    expect(chip.getAttribute('target')).toBe('_blank');
    expect(screen.queryByLabelText('Ver video')).toBeNull();
  });

  it('URL .mp4 suelta (autolink) es un chip con el nombre del archivo', () => {
    md(`Aquí está el resultado:\n\n${MP4}`);
    const chip = screen.getByRole('link', { name: 'output.mp4' });
    expect(chip.getAttribute('href')).toBe(MP4);
  });

  it('![alt](url.mp4) también queda como chip, nunca <img>', () => {
    md(`![clip.mp4](${MP4})`);
    expect(screen.getByRole('link', { name: 'clip.mp4' })).toBeTruthy();
    expect(screen.queryByRole('img')).toBeNull();
  });

  it('un enlace que no es video sigue siendo enlace normal', () => {
    md('[docs](https://example.com/page)');
    const a = screen.getByRole('link', { name: 'docs' });
    expect(a.getAttribute('href')).toBe('https://example.com/page');
    expect(a.getAttribute('target')).toBeNull();
  });

  it('una imagen sigue siendo <img> (GeneratedImage intacto)', () => {
    md('![gen.png](https://cdn.example.com/gen.png)');
    expect(screen.getByAltText('gen.png').getAttribute('src')).toBe(
      'https://cdn.example.com/gen.png'
    );
  });
});

// El <video> no tiene rol ARIA implícito; se localiza por su aria-label, que
// GeneratedVideo fija al rótulo del markdown o a "video generado".
describe('AgentMessageItem · el video es un adjunto del mensaje', () => {
  it('renderiza UN reproductor fuera de la lista y un chip en la viñeta', () => {
    item(REAL);
    const media = screen.getByTestId('message-media');
    const video = within(media).getByLabelText('video generado');
    expect(video.tagName).toBe('VIDEO');
    expect(video.getAttribute('src')).toBe(MP4);
    expect(video.hasAttribute('controls')).toBe(true);
    // Dentro de la lista solo queda el chip, ningún reproductor.
    const list = screen.getByRole('list');
    expect(within(list).queryByLabelText('video generado')).toBeNull();
    expect(within(list).getByRole('link', { name: 'output.mp4' })).toBeTruthy();
    expect(screen.getAllByLabelText('video generado')).toHaveLength(1);
  });

  it('usa el rótulo del enlace como etiqueta del reproductor', () => {
    item(`Aquí tienes:\n\n[Ver video](${SIGNED})`);
    const video = screen.getByLabelText('Ver video');
    expect(video.tagName).toBe('VIDEO');
    expect(video.getAttribute('src')).toBe(SIGNED);
  });

  it('la misma URL citada dos veces produce un solo reproductor', () => {
    item(`[Ver video](${MP4})\n\nEnlace directo: ${MP4}`);
    expect(screen.getAllByLabelText('Ver video')).toHaveLength(1);
  });

  it('un mensaje sin video no tiene bloque de adjuntos', () => {
    item('Solo texto y [docs](https://example.com).');
    expect(screen.queryByTestId('message-media')).toBeNull();
  });

  it('un mensaje humano con URL de video no genera adjunto', () => {
    item(`Mira ${MP4}`, true);
    expect(screen.queryByTestId('message-media')).toBeNull();
  });
});
