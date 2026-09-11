import { shouldAdmitFrame } from './useVoiceLive';

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
