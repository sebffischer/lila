import type { Api as CgApi } from '@lichess-org/chessground/api';
import { uciToMove } from '@lichess-org/chessground/util';
import type { Chess } from 'chessops/chess';
import { chessgroundDests } from 'chessops/compat';
import { makeFen } from 'chessops/fen';
import { makeSanAndPlay } from 'chessops/san';
import { parseSquare, parseUci } from 'chessops/util';

import { prop, type Prop } from 'lib';
import type { WithGround } from 'lib/game/ground';
import { PromotionCtrl } from 'lib/game/promotion';
import type { PuzPrefs } from 'lib/puz/interfaces';
import { sound } from 'lib/puz/util';
import { storedJsonProp } from 'lib/storage';

import type { DefenseLine, DefenseMode, DefenseOpts, DefensePuzzle } from './interfaces';
import { isSolution, normalUci, positionOf, refutationOf, startPosition } from './logic';

const lineDelay = 800;

export default class DefenseCtrl {
  readonly puzzles: DefensePuzzle[];
  readonly pref: PuzPrefs;
  readonly promotion: PromotionCtrl;
  readonly ground: Prop<CgApi | false> = prop<CgApi | false>(false);
  // first-try result of each puzzle, by id
  readonly results: Prop<Partial<Record<string, boolean>>> = storedJsonProp(
    'puzzle.defense.results',
    () => ({}),
  );

  index = 0;
  mode: DefenseMode = 'intro';
  pov: Color = 'white';
  pos: Chess; // the puzzle position, after the opponent's move
  wrong?: { uci: Uci; refutation: DefenseLine | null };
  threatShown = false;

  // what the board shows
  private shown: { pos: Chess; lastMove?: Uci; movable: boolean };
  private timers: ReturnType<typeof setTimeout>[] = [];

  constructor(
    opts: DefenseOpts,
    readonly redraw: Redraw,
  ) {
    this.puzzles = opts.puzzles;
    this.pref = { ...opts.pref, animation: opts.pref.animation.duration, ratings: false };
    this.promotion = new PromotionCtrl(this.withGround, () => this.setGround(), this.redraw);
    const unsolved = this.puzzles.findIndex(p => this.results()[p.id] === undefined);
    this.load(this.indexFromHash() ?? Math.max(0, unsolved));
    window.addEventListener('hashchange', () => {
      const index = this.indexFromHash();
      if (index !== undefined && index !== this.index) this.load(index);
    });
  }

  // #3 links to the third puzzle
  private indexFromHash(): number | undefined {
    const index = parseInt(location.hash.slice(1)) - 1;
    return index >= 0 && index < this.puzzles.length ? index : undefined;
  }

  get puzzle(): DefensePuzzle {
    return this.puzzles[this.index];
  }

  withGround: WithGround = f => {
    const g = this.ground();
    return g ? f(g) : undefined;
  };

  load = (index: number): void => {
    this.clearTimers();
    this.promotion.cancel();
    this.index = (index + this.puzzles.length) % this.puzzles.length;
    const p = this.puzzle;
    this.pos = startPosition(p);
    this.pov = this.pos.turn;
    this.mode = 'intro';
    this.wrong = undefined;
    this.threatShown = false;
    history.replaceState(null, '', `#${this.index + 1}`);
    this.show(positionOf(p.fen));
    this.later(600, () => {
      this.mode = 'play';
      this.playMove(positionOf(p.fen), p.move, true);
      this.redraw();
    });
    this.redraw();
  };

  next = (): void => this.load(this.index + 1);

  userMove = (orig: Key, dest: Key): void => {
    if (!this.promotion.start(orig, dest, { submit: this.playUserMove })) this.playUserMove(orig, dest);
  };

  private readonly playUserMove = (orig: Key, dest: Key, promotion?: Role): void => {
    if (this.mode !== 'play') return;
    const uci = normalUci(this.pos, { from: parseSquare(orig)!, to: parseSquare(dest)!, promotion })!;
    this.threatShown = false;
    if (isSolution(this.puzzle, this.pos, uci)) {
      this.mode = 'good';
      this.record(true);
      this.playMove(this.pos.clone(), uci, false);
      sound.good();
    } else {
      this.mode = 'fail';
      this.record(false);
      this.wrong = { uci, refutation: refutationOf(this.puzzle, this.pos, uci) };
      const after = this.playMove(this.pos.clone(), uci, false);
      sound.wrong();
      this.playLine(after, this.wrong.refutation?.line || []);
    }
    this.redraw();
  };

  retry = (): void => {
    this.clearTimers();
    this.mode = 'play';
    this.wrong = undefined;
    this.show(this.pos.clone(), this.puzzle.move, true);
    this.redraw();
  };

  viewSolution = (): void => {
    this.clearTimers();
    this.record(false);
    this.mode = 'solution';
    this.wrong = undefined;
    this.threatShown = false;
    this.show(this.pos.clone(), this.puzzle.move);
    this.later(lineDelay / 2, () => this.playMove(this.pos.clone(), this.puzzle.solution, false));
    this.redraw();
  };

  toggleThreat = (): void => {
    this.threatShown = !this.threatShown;
    this.setShapes();
    this.redraw();
  };

  resetResults = (): void => {
    this.results({});
    this.load(0);
  };

  firstTryWins = (): number => this.puzzles.filter(p => this.results()[p.id]).length;

  cgConfig = (): CgConfig => {
    const { pos, lastMove, movable } = this.shown;
    return {
      fen: makeFen(pos.toSetup()),
      orientation: this.pov,
      turnColor: pos.turn,
      check: pos.isCheck(),
      lastMove: lastMove ? uciToMove(lastMove) : undefined,
      movable: { color: movable ? this.pov : undefined, dests: movable ? chessgroundDests(pos) : new Map() },
    };
  };

  // Only the first attempt at each puzzle counts.
  private record(win: boolean): void {
    const results = this.results();
    if (results[this.puzzle.id] === undefined) this.results({ ...results, [this.puzzle.id]: win });
  }

  // Plays a move on `pos` (mutating it), shows it on the board and returns `pos`.
  private playMove(pos: Chess, uci: Uci, movable: boolean): Chess {
    const san = makeSanAndPlay(pos, parseUci(uci)!);
    site.sound.move({ uci, san });
    this.show(pos, uci, movable);
    return pos;
  }

  private playLine(from: Chess, line: Uci[]): void {
    const pos = from.clone();
    line.forEach((uci, i) => this.later(lineDelay * (i + 1), () => this.playMove(pos, uci, false)));
  }

  private show(pos: Chess, lastMove?: Uci, movable = false): void {
    this.shown = { pos: pos.clone(), lastMove, movable };
    this.setGround();
    this.setShapes();
  }

  private setGround(): void {
    this.withGround(g => g.set(this.cgConfig()));
  }

  private setShapes(): void {
    const threat = this.threatShown && this.mode === 'play' ? this.puzzle.threat?.line[0] : undefined;
    this.withGround(g =>
      g.setAutoShapes(
        threat ? [{ orig: threat.slice(0, 2) as Key, dest: threat.slice(2, 4) as Key, brush: 'red' }] : [],
      ),
    );
  }

  private later(ms: number, f: () => void): void {
    this.timers.push(setTimeout(f, ms));
  }

  private clearTimers(): void {
    this.timers.forEach(clearTimeout);
    this.timers = [];
  }
}
