import { render, screen } from '@testing-library/react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  isVideoUrl,
  markdownComponents,
  videoFilename,
} from './StreamingAgentMessage';

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

// El <video> no tiene rol ARIA implícito; se localiza por su aria-label, que
// GeneratedVideo fija al alt/label del markdown.
describe('markdownComponents · video', () => {
  it('![alt](url.mp4) renderiza <video controls> y no <img>', () => {
    md(`![coche.mp4](${MP4})`);
    const v = screen.getByLabelText('coche.mp4');
    expect(v.tagName).toBe('VIDEO');
    expect(v.getAttribute('src')).toBe(MP4);
    expect(v.hasAttribute('controls')).toBe(true);
    expect(screen.queryByRole('img')).toBeNull();
  });

  it('[label](url.mp4) renderiza <video> y no un <a>', () => {
    md(`[Ver video](${SIGNED})`);
    const v = screen.getByLabelText('Ver video');
    expect(v.tagName).toBe('VIDEO');
    expect(v.getAttribute('src')).toBe(SIGNED);
    expect(screen.queryByRole('link')).toBeNull();
  });

  it('URL .mp4 suelta (autolink de remark-gfm) renderiza <video>', () => {
    md(`Aquí está el resultado:\n\n${MP4}`);
    const v = screen.getByLabelText(MP4);
    expect(v.tagName).toBe('VIDEO');
    expect(v.getAttribute('src')).toBe(MP4);
    expect(screen.queryByRole('link')).toBeNull();
  });

  it('un enlace que no es video sigue siendo <a>', () => {
    md('[docs](https://example.com/page)');
    expect(screen.getByRole('link', { name: 'docs' }).getAttribute('href')).toBe(
      'https://example.com/page'
    );
    expect(screen.queryByLabelText('docs')).toBeNull();
  });

  it('una imagen sigue siendo <img> (GeneratedImage intacto)', () => {
    md('![gen.png](https://cdn.example.com/gen.png)');
    expect(screen.getByAltText('gen.png').getAttribute('src')).toBe(
      'https://cdn.example.com/gen.png'
    );
    expect(screen.queryByLabelText('gen.png')).toBeNull();
  });
});
