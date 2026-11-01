import type { Coords } from 'lib/prefs';

// What a move leads to, from the defender's point of view, as computed by
// bin/defense-puzzles/generate.py. `line` starts with the opponent's reply.
export interface DefenseLine {
  line: Uci[];
  mate?: number; // the opponent mates in this many moves
  material?: number; // net material the defender loses along the line, in centipawns
  eval: number; // engine eval for the defender, in centipawns
}

export interface DefensePuzzle {
  id: string;
  fen: FEN; // the position before the opponent's threatening move
  move: Uci; // the opponent's threatening move
  solution: Uci; // the only move that doesn't lose
  threat: DefenseLine | null; // what the opponent would do if the defender could pass; null when in check
  refutations: Record<Uci, DefenseLine>; // one entry per wrong legal move
  game: { id: string; ply: number; white: string; black: string };
}

export interface DefensePref {
  coords: Coords;
  is3d: boolean;
  destination: boolean;
  rookCastle: boolean;
  moveEvent: number;
  highlight: boolean;
  animation: { duration: number };
}

export interface DefenseOpts {
  puzzles: DefensePuzzle[];
  pref: DefensePref;
}

// intro: the opponent's move is about to be played
// play: waiting for the defender's move
// good: the defender found the move
// fail: the defender played a losing move, and its refutation is being shown
// solution: the defender asked for the solution
export type DefenseMode = 'intro' | 'play' | 'good' | 'fail' | 'solution';
