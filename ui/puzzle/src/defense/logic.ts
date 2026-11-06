import { Chess, normalizeMove, type Position } from 'chessops/chess';
import { parseFen } from 'chessops/fen';
import { makeSanAndPlay } from 'chessops/san';
import type { Move } from 'chessops/types';
import { makeUci, opposite, parseUci } from 'chessops/util';

import type { DefenseLine, DefensePuzzle, DefenseStep } from './interfaces';

export const positionOf = (fen: FEN): Chess => Chess.fromSetup(parseFen(fen).unwrap()).unwrap();

// The puzzle position: right after the opponent's threatening move.
export function startPosition(p: DefensePuzzle): Chess {
  const pos = positionOf(p.fen);
  pos.play(parseUci(p.move)!);
  return pos;
}

// Canonical UCI of a move, so that both ways of castling (e1g1 and e1h1) compare equal.
export const normalUci = (pos: Position, move: Move | Uci): Uci | undefined => {
  const m = typeof move === 'string' ? parseUci(move) : move;
  return m && makeUci(normalizeMove(pos, m));
};

export const isSolution = (step: DefenseStep, pos: Position, uci: Uci): boolean =>
  normalUci(pos, step.solution) === uci;

export function refutationOf(step: DefenseStep, pos: Position, uci: Uci): DefenseLine | null {
  for (const [key, line] of Object.entries(step.refutations)) if (normalUci(pos, key) === uci) return line;
  return null;
}

// The rest of the defence from step `from` on: the defender's moves and the opponent's replies.
export const solutionLine = (p: DefensePuzzle, from = 0): Uci[] =>
  p.steps
    .slice(from)
    .flatMap((s, i, rest) => (s.reply && i + 1 < rest.length ? [s.solution, s.reply] : [s.solution]));

// The same position with the opponent to move, to show what it threatens.
export function passPosition(pos: Chess): Chess | undefined {
  const setup = pos.toSetup();
  return Chess.fromSetup({
    ...setup,
    turn: opposite(setup.turn),
    epSquare: undefined,
    fullmoves: setup.turn === 'black' ? setup.fullmoves + 1 : setup.fullmoves,
  }).unwrap(
    p => p,
    () => undefined,
  );
}

// Moves in SAN with move numbers, like "23... Nd4 24. Qd1 Nxe2+".
// `firstSuffix` annotates the first move, as in "24. Kf1? Nd2+".
export function sanLine(pos: Position, line: Uci[], firstSuffix = ''): string {
  const p = pos.clone();
  const sans: string[] = [];
  for (const uci of line) {
    const move = parseUci(uci);
    if (!move || !p.isLegal(normalizeMove(p, move))) break;
    const num = p.turn === 'white' ? `${p.fullmoves}. ` : sans.length === 0 ? `${p.fullmoves}... ` : '';
    sans.push(num + makeSanAndPlay(p, move) + (sans.length === 0 ? firstSuffix : ''));
  }
  return sans.join(' ');
}
