import { resolveTranscript, shouldAdmitFrame } from './useVoiceLive';

// Regla de admisión de frames a la cola de reproducción. Cada caso es una
// situación observada en el contrato de Playwright (T4/T5/T5b/T8) o en el
// relay real: el server sigue vaciando deltas de una respuesta cancelada
// durante segundos, y esos frames NO deben sonar.
describe('shouldAdmitFrame', () => {
  it('admite el frame de la respuesta viva (rid y turno coinciden)', () => {
    expect(
      shouldAdmitFrame({ rid: 'B', playingRid: 'B', playingTurn: 5, liveTurn: 5 })
    ).toBe(true);
  });

  it('T5b: descarta frames tardíos del ACK cancelado cuando el say ya tiene el altavoz', () => {
    expect(
      shouldAdmitFrame({ rid: 'A', playingRid: 'B', playingTurn: 5, liveTurn: 5 })
    ).toBe(false);
  });

  it('T8: tras barge-in (sin respuesta viva) un frame etiquetado tardío NO entra', () => {
    expect(
      shouldAdmitFrame({ rid: 'A', playingRid: '', playingTurn: 0, liveTurn: 5 })
    ).toBe(false);
  });

  it('T5: descarta frames de una respuesta cuyo turno ya no es el vivo', () => {
    expect(
      shouldAdmitFrame({ rid: 'A', playingRid: 'A', playingTurn: 3, liveTurn: 4 })
    ).toBe(false);
  });

  it('binario sin etiqueta: solo aplica la guarda por turno', () => {
    expect(
      shouldAdmitFrame({ playingRid: 'X', playingTurn: 2, liveTurn: 2 })
    ).toBe(true);
    expect(
      shouldAdmitFrame({ playingRid: 'X', playingTurn: 2, liveTurn: 3 })
    ).toBe(false);
    expect(
      shouldAdmitFrame({ playingRid: '', playingTurn: 0, liveTurn: 3 })
    ).toBe(true);
  });
});

// Enunciado partido por el VAD (visto en la 0110): "…los contextos del prime"
// + 500 ms de pausa + "Terminan con cero f…" → dos turnos, el router recibió
// medio enunciado y contestó "no puedo consultar GitHub".
describe('resolveTranscript', () => {
  const prev = (over: Partial<Parameters<typeof resolveTranscript>[0]>) => ({
    state: 'interrupted' as const,
    answered: false,
    userText: 'valida los contextos del primer',
    ...over,
  });

  it('turno interrumpido ANTES de que el router hablara → continuación fusionada', () => {
    expect(resolveTranscript(prev({}), 'commit con cero f')).toEqual({
      text: 'valida los contextos del primer commit con cero f',
      continued: true,
    });
  });

  it('el router ya había producido texto → es interrupción, turno nuevo', () => {
    expect(resolveTranscript(prev({ answered: true }), 'otra cosa')).toEqual({
      text: 'otra cosa',
      continued: false,
    });
  });

  it('turno anterior consumido por speak, cerrado o sin sesión → turno nuevo', () => {
    for (const state of ['spoken', 'open', 'none'] as const) {
      expect(resolveTranscript(prev({ state }), 'hola').continued).toBe(false);
    }
  });

  it('sin texto previo no hay nada que fusionar', () => {
    expect(resolveTranscript(prev({ userText: '  ' }), 'hola')).toEqual({
      text: 'hola',
      continued: false,
    });
  });
});
