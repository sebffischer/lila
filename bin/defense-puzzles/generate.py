#!/usr/bin/env python3
"""
Find defensive puzzles in played games.

A defensive puzzle is a position where the side to move faces a threat and
exactly one legal move avoids losing material (or getting mated). The
puzzle starts one ply earlier, so the board can show the opponent's move
that created the threat, like regular lichess puzzles do.

A position right after the opponent's move qualifies when:

  1. There is a real threat: if the side to move could pass (null move),
     the opponent would gain at least --margin centipawns.
  2. Exactly one move holds: the best move is at least --margin centipawns
     better than every other legal move, and there are at least
     --min-legal legal moves.
  3. The move is a defence, not a counter-attack: its eval is within
     --max-gain of the current material balance, and the defender is not
     already lost (eval between --min-eval and --max-eval).

The checks run in two stages: a shallow multipv-2 search filters
positions cheaply, then a deep search with one PV per legal move confirms
the survivors and records, for every wrong move, the line that refutes it.

Usage:

  # 1. scan games for candidates (JSON lines, one candidate per line)
  generate.py scan --pgn games.pgn.zst --stockfish /path/to/stockfish \
      --games 2000 --out candidates.jsonl

  # 2. re-verify chosen candidates at a higher depth and export them in
  #    the format served by /training/defense
  generate.py export --stockfish /path/to/stockfish --candidates candidates.jsonl \
      --ids abcd1234-31,efgh5678-40 --out modules/puzzle/src/main/resources/defensePuzzles.json
  pnpm format  # the repo's formatter also covers the exported JSON

Requires python-chess (and zstandard to read .zst files).
"""

import argparse
import io
import json
import multiprocessing as mp
import sys

import chess
import chess.engine
import chess.pgn

VALUES = {chess.PAWN: 100, chess.KNIGHT: 300, chess.BISHOP: 300, chess.ROOK: 500, chess.QUEEN: 900}
MATE = 100_000


def material(board, color):
    return sum(
        VALUES.get(p.piece_type, 0) * (1 if p.color == color else -1) for p in board.piece_map().values()
    )


def cp(score, color):
    return score.pov(color).score(mate_score=MATE)


def mated_in(score, color):
    """Number of opponent moves until `color` is mated, else None."""
    m = score.pov(color).mate()
    return -m if m is not None and m < 0 else None


def null_move(board):
    """The same position with the other side to move (en passant cleared)."""
    b = board.copy(stack=False)
    b.push(chess.Move.null())
    return chess.Board(b.fen())


def explain(board, line, defender, base, mate, max_plies=8):
    """Trim an engine line (opponent to move first) to the part that shows
    what the defender loses, and measure the net material lost.

    `base` is the defender's material balance in the puzzle position. The
    loss is measured at quiet points (after the defender's replies), and the
    line is cut right after the opponent's move that settles it.
    """
    if mate is not None:
        return {"line": [m.uci() for m in line[: 2 * mate - 1]], "mate": mate}
    b = board.copy(stack=False)
    worst, cut = base, min(len(line), 3)
    for k, move in enumerate(line[:max_plies], 1):
        if not b.is_legal(move):
            break
        b.push(move)
        if k % 2 == 0 and material(b, defender) < worst:
            worst, cut = material(b, defender), k - 1
    return {"line": [m.uci() for m in line[:cut]], "material": base - worst}


class Analyzer:
    def __init__(self, path, threads=1, hash_mb=64):
        self.engine = chess.engine.SimpleEngine.popen_uci(path)
        self.engine.configure({"Threads": threads, "Hash": hash_mb})

    def analyse(self, board, depth, multipv=1):
        infos = self.engine.analyse(board, chess.engine.Limit(depth=depth), multipv=multipv)
        return [i for i in infos if i.get("pv")]

    def close(self):
        self.engine.quit()


def quick_filter(an, board, cfg):
    """Cheap stage: is this position worth a deep search?"""
    color = board.turn
    if board.legal_moves.count() < cfg.min_legal:
        return False
    infos = an.analyse(board, cfg.quick_depth, 2)
    if len(infos) < 2:
        return False
    e1, e2 = cp(infos[0]["score"], color), cp(infos[1]["score"], color)
    slack = 75  # shallow evals are noisy; let borderline positions through
    if not cfg.min_eval - slack <= e1 <= cfg.max_eval + slack:
        return False
    if e1 - e2 < cfg.margin - slack or e1 - material(board, color) > cfg.max_gain + slack:
        return False
    if not board.is_check():
        nb = null_move(board)
        if not nb.is_valid():
            return False
        infos = an.analyse(nb, cfg.quick_depth)
        if not infos or cp(infos[0]["score"], color) > e1 - (cfg.margin - slack):
            return False
    return True


def deep_check(an, board, cfg, depth):
    """Full stage: evaluate every legal move. Returns the puzzle data, or None."""
    color = board.turn
    legal = list(board.legal_moves)
    infos = an.analyse(board, depth, len(legal))
    if len(infos) != len(legal):
        return None
    scored = sorted(((i["pv"], i["score"]) for i in infos), key=lambda x: -cp(x[1], color))
    (best_pv, best_score), (_, second_score) = scored[0], scored[1]
    e1, e2 = cp(best_score, color), cp(second_score, color)
    base = material(board, color)
    if not cfg.min_eval <= e1 <= cfg.max_eval or e1 - e2 < cfg.margin or e1 - base > cfg.max_gain:
        return None

    threat = None
    if not board.is_check():
        nb = null_move(board)
        if not nb.is_valid():
            return None
        t = an.analyse(nb, depth)
        if not t or cp(t[0]["score"], color) > e1 - cfg.margin:
            return None
        threat = explain(nb, t[0]["pv"], color, base, mated_in(t[0]["score"], color))
        threat["eval"] = cp(t[0]["score"], color)

    refutations = {}
    for pv, score in scored[1:]:
        after = board.copy(stack=False)
        after.push(pv[0])
        r = explain(after, pv[1:], color, base, mated_in(score, color))
        r["eval"] = cp(score, color)
        refutations[pv[0].uci()] = r

    return {
        "solution": best_pv[0].uci(),
        "eval": e1,
        "gap": e1 - e2,
        "legal": len(legal),
        "threat": threat,
        "refutations": refutations,
    }


# ---------------------------------------------------------------- scanning

_an = None
_cfg = None


def _init_worker(cfg):
    global _an, _cfg
    _cfg = cfg
    _an = Analyzer(cfg.stockfish)


def _scan_game(task):
    headers, moves = task
    board = chess.Board()
    found = []
    game_id = headers.get("Site", "").rsplit("/", 1)[-1]
    for ply, uci in enumerate(moves, 1):
        before = board.fen()
        board.push(chess.Move.from_uci(uci))
        if ply < _cfg.min_ply or board.is_game_over():
            continue
        if not quick_filter(_an, board, _cfg):
            continue
        res = deep_check(_an, board, _cfg, _cfg.depth)
        if res:
            found.append(
                {
                    "id": f"{game_id}-{ply}",
                    "fen": before,
                    "move": uci,
                    **res,
                    "game": {
                        "id": game_id,
                        "ply": ply,
                        "white": headers.get("White"),
                        "black": headers.get("Black"),
                        "whiteElo": headers.get("WhiteElo"),
                        "blackElo": headers.get("BlackElo"),
                        "date": headers.get("UTCDate") or headers.get("Date"),
                        "event": headers.get("Event"),
                    },
                }
            )
    return found


def open_pgn(path):
    if path.endswith(".zst"):
        import zstandard

        fh = open(path, "rb")
        return io.TextIOWrapper(zstandard.ZstdDecompressor().stream_reader(fh), encoding="utf-8")
    return open(path, encoding="utf-8")


def read_games(cfg):
    n = 0
    with open_pgn(cfg.pgn) as pgn:
        while n < cfg.games:
            game = chess.pgn.read_game(pgn)
            if game is None:
                return
            h = game.headers
            try:
                elos = int(h.get("WhiteElo", 0)), int(h.get("BlackElo", 0))
            except ValueError:
                continue
            if min(elos) < cfg.min_elo or h.get("Variant", "Standard") != "Standard" or "FEN" in h:
                continue
            moves = [m.uci() for m in game.mainline_moves()]
            if len(moves) < 2 * cfg.min_ply:
                continue
            n += 1
            yield dict(h), moves


def scan(cfg):
    out = open(cfg.out, "w")
    games = found = 0
    with mp.Pool(cfg.workers, _init_worker, (cfg,)) as pool:
        for res in pool.imap_unordered(_scan_game, read_games(cfg), chunksize=1):
            games += 1
            for p in res:
                out.write(json.dumps(p) + "\n")
                found += 1
            out.flush()
            if games % 50 == 0:
                print(f"{games} games, {found} candidates", file=sys.stderr, flush=True)
    print(f"done: {games} games, {found} candidates -> {cfg.out}", file=sys.stderr)


# ---------------------------------------------------------------- export


def export(cfg):
    cands = {}
    with open(cfg.candidates) as f:
        for line in f:
            c = json.loads(line)
            cands[c["id"]] = c
    an = Analyzer(cfg.stockfish, threads=cfg.threads, hash_mb=512)
    puzzles = []
    for pid in cfg.ids.split(","):
        c = cands[pid]
        board = chess.Board(c["fen"])
        board.push(chess.Move.from_uci(c["move"]))
        res = deep_check(an, board, cfg, cfg.verify_depth)
        if not res:
            print(f"{pid}: rejected at depth {cfg.verify_depth}", file=sys.stderr)
            continue
        if res["solution"] != c["solution"]:
            print(f"{pid}: solution changed {c['solution']} -> {res['solution']}", file=sys.stderr)
        g = c["game"]
        puzzles.append(
            {
                "id": pid,
                "fen": c["fen"],
                "move": c["move"],
                "solution": res["solution"],
                "threat": res["threat"],
                "refutations": res["refutations"],
                "game": {"id": g["id"], "ply": g["ply"], "white": g["white"], "black": g["black"]},
            }
        )
        print(f"{pid}: ok (gap {res['gap']}, eval {res['eval']})", file=sys.stderr)
    an.close()
    with open(cfg.out, "w") as f:
        json.dump(puzzles, f, indent=1)
        f.write("\n")
    print(f"exported {len(puzzles)} puzzles -> {cfg.out}", file=sys.stderr)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--stockfish", required=True)
    p.add_argument("--margin", type=int, default=200, help="min cp gap between the only move and the rest")
    p.add_argument("--max-gain", type=int, default=200, help="max cp the only move may gain over material")
    p.add_argument("--min-eval", type=int, default=-200)
    p.add_argument("--max-eval", type=int, default=500)
    p.add_argument("--min-legal", type=int, default=4)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan")
    s.add_argument("--pgn", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--games", type=int, default=1000)
    s.add_argument("--min-elo", type=int, default=1600)
    s.add_argument("--min-ply", type=int, default=12)
    s.add_argument("--workers", type=int, default=max(1, mp.cpu_count() // 2))
    s.add_argument("--quick-depth", type=int, default=10)
    s.add_argument("--depth", type=int, default=16)

    e = sub.add_parser("export")
    e.add_argument("--candidates", required=True)
    e.add_argument("--ids", required=True)
    e.add_argument("--out", required=True)
    e.add_argument("--verify-depth", type=int, default=22)
    e.add_argument("--threads", type=int, default=8)

    cfg = p.parse_args()
    scan(cfg) if cfg.cmd == "scan" else export(cfg)


if __name__ == "__main__":
    main()
