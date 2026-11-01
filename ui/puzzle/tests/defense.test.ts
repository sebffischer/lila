import { normalizeMove } from 'chessops/chess';
import { makeUci, parseUci } from 'chessops/util';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { describe, test } from 'node:test';

import type { DefensePuzzle } from '../src/defense/interfaces';
import {
  isSolution,
  normalUci,
  passPosition,
  positionOf,
  sanLine,
  startPosition,
} from '../src/defense/logic';

const puzzles: DefensePuzzle[] = JSON.parse(
  readFileSync(
    new URL('../../../modules/puzzle/src/main/resources/defensePuzzles.json', import.meta.url),
    'utf8',
  ),
);

const legal = (pos: ReturnType<typeof positionOf>, uci: Uci): boolean => {
  const move = parseUci(uci);
  return !!move && pos.isLegal(normalizeMove(pos, move));
};

const playLine = (pos: ReturnType<typeof positionOf>, line: Uci[]): boolean =>
  line.every(uci => {
    if (!legal(pos, uci)) return false;
    pos.play(parseUci(uci)!);
    return true;
  });

describe('defense puzzle logic', () => {
  test('writes lines in SAN with move numbers', () => {
    const pos = positionOf('rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1');
    assert.equal(sanLine(pos, ['e7e5', 'g1f3', 'b8c6']), '1... e5 2. Nf3 Nc6');
    assert.equal(sanLine(pos, ['f7f6', 'd1h5'], '?'), '1... f6? 2. Qh5+');
  });

  test('stops at the first illegal move', () => {
    const pos = positionOf('rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1');
    assert.equal(sanLine(pos, ['e7e5', 'e7e5']), '1... e5');
  });

  test('treats both castling notations as the same move', () => {
    const pos = positionOf('r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1');
    assert.equal(normalUci(pos, 'e1g1'), normalUci(pos, 'e1h1'));
    assert.equal(sanLine(pos, ['e1g1']), '1. O-O');
  });

  test('passing gives the move to the opponent', () => {
    const black = passPosition(positionOf('4k3/8/8/8/8/8/8/4K3 b - - 0 23'))!;
    assert.equal(black.turn, 'white');
    assert.equal(black.fullmoves, 24);
    const white = passPosition(positionOf('4k3/8/8/8/8/8/8/4K3 w - - 0 23'))!;
    assert.equal(white.turn, 'black');
    assert.equal(white.fullmoves, 23);
  });
});

describe('defense puzzle data', () => {
  test('has 10 puzzles with distinct ids', () => {
    assert.equal(puzzles.length, 10);
    assert.equal(new Set(puzzles.map(p => p.id)).size, puzzles.length);
  });

  for (const p of puzzles) {
    test(`puzzle ${p.id} is consistent`, () => {
      const before = positionOf(p.fen);
      assert.ok(legal(before, p.move), 'the opponent move is legal');
      const pos = startPosition(p);
      assert.ok(legal(pos, p.solution), 'the solution is legal');
      assert.ok(isSolution(p, pos, normalUci(pos, p.solution)!));

      // every legal move but the solution has a refutation, and nothing else does
      const wrong = [...pos.allDests()].flatMap(([from, tos]) =>
        [...tos].map(to => makeUci(normalizeMove(pos, { from, to }))),
      );
      const refuted = Object.keys(p.refutations).map(uci => normalUci(pos, uci));
      const expected = wrong.filter(uci => uci !== normalUci(pos, p.solution));
      assert.ok(expected.length >= 3, 'at least 3 wrong moves');
      // promotions: chessops dests list one move per square, the data one per piece
      assert.deepEqual(new Set(refuted.map(u => u!.slice(0, 4))), new Set(expected));

      for (const [uci, r] of Object.entries(p.refutations)) {
        const after = pos.clone();
        after.play(parseUci(uci)!);
        assert.ok(r.line.length > 0, `${uci} has a refutation`);
        assert.ok(playLine(after, r.line), `${uci} refutation is legal`);
        assert.ok(r.mate || (r.material ?? 0) > 0 || r.eval < -150, `${uci} loses something`);
      }

      if (pos.isCheck()) assert.equal(p.threat, null, 'no threat line when in check');
      else {
        assert.ok(p.threat, 'there is a threat');
        assert.ok(playLine(passPosition(pos)!, p.threat.line), 'the threat line is legal');
      }
    });
  }
});
