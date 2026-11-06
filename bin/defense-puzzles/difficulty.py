#!/usr/bin/env python3
"""
Estimate how hard a defense puzzle is, on the lichess puzzle rating scale.

The features come from Maia-2, a model of the moves human players choose
at a given rating: for each of the solver's moves and a range of ratings,
the probability that a player of that rating plays it. Long puzzles are
harder, so the features also cover the whole line (the product and the
minimum of the per-move probabilities).

A gradient-boosted regression maps the features to a rating. It is trained
on lichess puzzles whose ratings are well established (many plays, low
rating deviation), stratified over the rating range so hard puzzles are
not drowned out by the many easy ones.

Defense puzzles differ from regular puzzles (the solver parries a threat
instead of winning material), so `validate` checks the estimates against
what actually happened in the games the positions come from: the harder
the puzzle, the less often the player should have found the move.

How well it works (November 2026, 6,499 lichess puzzles, 5-fold cross
validation): mean absolute error 284 rating points, Spearman correlation
0.78. Like any regression it pulls estimates toward the middle: puzzles
rated 2400-2700 on lichess get about 2160 on average, so read the top of
the scale as "2100 and up". On 2,543 positions from October 2026 games,
players rated ~2000 found the only move 98% of the time below an estimate
of 800, and 30% of the time above 2400; adding the estimate to the
player's rating raises the AUC for predicting that from 0.61 to 0.78.

Usage:

  # 1. train the rating model (once)
  difficulty.py calibrate --puzzles lichess_db_puzzle.csv.zst --model difficulty-model.pkl

  # 2. rate defense puzzles: scan candidates (JSON lines) or an export (JSON array)
  difficulty.py rate --model difficulty-model.pkl --input candidates.jsonl --output rated.jsonl

  # 3. compare ratings with what the players did in their games
  difficulty.py validate --input rated.jsonl

  # 4. pick puzzles spread over rating bands, for generate.py export --ids
  difficulty.py pick --input rated.jsonl --bands 600-1400,1400-1900,1900-3000 --per-band 10

Requires maia2 (which brings PyTorch), scikit-learn, python-chess and
zstandard.
"""

import argparse
import csv
import io
import json
import math
import pickle
import random
import sys
from collections import defaultdict

import chess
import numpy as np

from generate import fair  # same directory

ELOS = [1000, 1200, 1400, 1600, 1800, 2000]  # Maia-2 knows ratings from <1100 to >=2000
EPS = 1e-4


def logit(p):
    p = min(max(p, EPS), 1 - EPS)
    return math.log(p / (1 - p))


class Maia:
    """Probability that a player of a given rating plays a given move."""

    def __init__(self, models_dir, threads, kind="rapid", batch=256):
        import torch
        from maia2 import inference, model, utils

        torch.set_num_threads(threads)

        self.torch, self.utils, self.inference = torch, utils, inference
        self.model = model.from_pretrained(type=kind, device="cpu", save_root=models_dir).eval()
        self.moves_dict, self.elo_dict, _ = inference.prepare()
        self.batch = batch

    def probs(self, items):
        """items: (fen, uci) pairs. Returns, for each, an array over ELOS of
        [P(uci), P(most likely move)]."""
        torch = self.torch
        out = []
        step = max(1, self.batch // len(ELOS))
        for i in range(0, len(items), step):
            if i and i % (step * 50) == 0:
                print(f"maia: {i}/{len(items)} positions", file=sys.stderr, flush=True)
            chunk = items[i : i + step]
            boards, elos, masks, idx = [], [], [], []
            for fen, uci in chunk:
                black = fen.split(" ")[1] == "b"
                b, _, _, legal = self.inference.preprocessing(fen, 0, 0, self.elo_dict, self.moves_dict)
                for elo in ELOS:
                    boards.append(b)
                    elos.append(self.utils.map_to_category(elo, self.elo_dict))
                    masks.append(legal)
                idx.append(self.moves_dict[self.utils.mirror_move(uci) if black else uci])
            with torch.no_grad():
                e = torch.tensor(elos)
                logits, _, _ = self.model(torch.stack(boards), e, e)
                p = logits.masked_fill(~torch.stack(masks).bool(), float("-inf")).softmax(-1)
            p = p.view(len(chunk), len(ELOS), -1)
            for k, move_idx in enumerate(idx):
                out.append(np.stack([p[k, :, move_idx].numpy(), p[k].max(-1).values.numpy()], -1))
        return out


def solver_positions(fen, moves):
    """(fen, solution) for each solver move of a lichess-style line: `fen`
    is before the opponent's first move, `moves` alternate opponent and
    solver, starting with the opponent."""
    board = chess.Board(fen)
    out = []
    for k, uci in enumerate(moves):
        if k % 2 == 1:
            out.append((board.fen(), uci))
        board.push_uci(uci)
    return out


def defence_moves(p):
    """The lichess-style move list of a defense puzzle (candidate or export)."""
    moves = [p["move"]]
    for s in p["steps"]:
        moves.append(s["solution"])
        if s.get("reply"):
            moves.append(s["reply"])
    return moves


FEATURES = (
    [f"first_{e}" for e in ELOS]
    + [f"line_{e}" for e in ELOS]
    + [f"min_{e}" for e in ELOS]
    + [f"top_{e}" for e in ELOS]
    + ["steps", "legal", "check"]
)


def features(positions, probs):
    """Feature vector of one puzzle: its solver positions and Maia output."""
    first = probs[0]
    line = np.prod([p[:, 0] for p in probs], axis=0)
    low = np.min([p[:, 0] for p in probs], axis=0)
    board = chess.Board(positions[0][0])
    return (
        [logit(x) for x in first[:, 0]]
        + [logit(x) for x in line]
        + [logit(x) for x in low]
        # how tempting the most likely move is, when it isn't the solution
        + [logit(top) if top > sol else 0.0 for sol, top in first]
        + [len(positions), board.legal_moves.count(), int(board.is_check())]
    )


def featurize(maia, puzzles):
    """puzzles: lists of solver positions. Returns the feature matrix."""
    flat = [pos for positions in puzzles for pos in positions]
    probs = maia.probs(flat)
    X, k = [], 0
    for positions in puzzles:
        X.append(features(positions, probs[k : k + len(positions)]))
        k += len(positions)
    return np.array(X)


# ---------------------------------------------------------------- calibrate


def read_lichess_puzzles(path, n, seed, max_rd, min_plays):
    """A sample of well-rated lichess puzzles, up to n / 25 per 100 points."""
    import zstandard

    per_bin = n // 25
    rng = random.Random(seed)
    bins = defaultdict(list)
    seen = defaultdict(int)
    with open(path, "rb") as fh:
        rows = csv.DictReader(io.TextIOWrapper(zstandard.ZstdDecompressor().stream_reader(fh), encoding="utf-8"))
        for row in rows:
            if int(row["RatingDeviation"]) > max_rd or int(row["NbPlays"]) < min_plays:
                continue
            b = int(row["Rating"]) // 100
            seen[b] += 1
            # reservoir sampling per rating bin
            if len(bins[b]) < per_bin:
                bins[b].append(row)
            else:
                j = rng.randrange(seen[b])
                if j < per_bin:
                    bins[b][j] = row
    return [row for b in sorted(bins) for row in bins[b]]


def calibrate(cfg):
    from scipy.stats import spearmanr
    from sklearn.ensemble import HistGradientBoostingRegressor
    from sklearn.model_selection import KFold, cross_val_predict

    cache = f"{cfg.puzzles}.sample-{cfg.n}-{cfg.seed}-{cfg.max_rd}-{cfg.min_plays}.jsonl"
    try:
        with open(cache) as f:
            rows = [json.loads(line) for line in f]
    except FileNotFoundError:
        rows = read_lichess_puzzles(cfg.puzzles, cfg.n, cfg.seed, cfg.max_rd, cfg.min_plays)
        with open(cache, "w") as f:
            f.writelines(json.dumps(r) + "\n" for r in rows)
    print(f"{len(rows)} puzzles sampled", file=sys.stderr)
    positions = [solver_positions(r["FEN"], r["Moves"].split()) for r in rows]
    maia = Maia(cfg.maia_models, cfg.threads)
    X = featurize(maia, positions)
    y = np.array([int(r["Rating"]) for r in rows])

    model = HistGradientBoostingRegressor(max_iter=400, learning_rate=0.05, random_state=cfg.seed)
    pred = cross_val_predict(model, X, y, cv=KFold(5, shuffle=True, random_state=cfg.seed))
    err = pred - y
    print(
        f"5-fold CV: MAE {np.abs(err).mean():.0f}, RMSE {np.sqrt((err**2).mean()):.0f}, "
        f"Spearman {spearmanr(pred, y).statistic:.3f}, rating SD {y.std():.0f}",
        file=sys.stderr,
    )
    for lo in range(600, 2800, 300):
        m = (y >= lo) & (y < lo + 300)
        if m.any():
            print(f"  true {lo}-{lo + 299}: mean prediction {pred[m].mean():.0f} (n={m.sum()})", file=sys.stderr)
    model.fit(X, y)
    with open(cfg.model, "wb") as f:
        pickle.dump({"model": model, "features": FEATURES, "elos": ELOS}, f)
    with open(cfg.model + ".data.jsonl", "w") as f:
        for r, x, p in zip(rows, X, pred):
            f.write(json.dumps({"id": r["PuzzleId"], "rating": int(r["Rating"]), "cv": round(float(p)), "themes": r["Themes"], "x": [round(v, 3) for v in x]}) + "\n")
    print(f"model -> {cfg.model}", file=sys.stderr)


# ---------------------------------------------------------------- rate


def read_any(path):
    with open(path) as f:
        text = f.read()
    if text.lstrip().startswith("["):
        return json.loads(text), True
    return [json.loads(line) for line in text.splitlines() if line.strip()], False


def rate(cfg):
    with open(cfg.model, "rb") as f:
        saved = pickle.load(f)
    assert saved["features"] == FEATURES and saved["elos"] == ELOS, "model built with other features"
    puzzles, is_array = read_any(cfg.input)
    positions = [solver_positions(p["fen"], defence_moves(p)) for p in puzzles]
    X = featurize(Maia(cfg.maia_models, cfg.threads), positions)
    ratings = saved["model"].predict(X)
    for p, x, r in zip(puzzles, X, ratings):
        p["rating"] = int(round(r / 10) * 10)
        if not is_array:  # keep the Maia probabilities of the first move for analysis
            p["maia"] = [round(1 / (1 + math.exp(-x[i])), 4) for i in range(len(ELOS))]
    with open(cfg.output, "w") as f:
        if is_array:
            json.dump(puzzles, f, indent=1)
            f.write("\n")
        else:
            for p in puzzles:
                f.write(json.dumps(p) + "\n")
    print(f"rated {len(puzzles)} puzzles -> {cfg.output}", file=sys.stderr)


# ---------------------------------------------------------------- validate


def validate(cfg):
    """Do players miss the harder puzzles more often in their games?"""
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import log_loss, roc_auc_score
    from sklearn.model_selection import StratifiedKFold, cross_val_predict

    puzzles, _ = read_any(cfg.input)
    puzzles = [p for p in puzzles if p.get("played") and "rating" in p]
    found = np.array([p["played"] == p["steps"][0]["solution"] for p in puzzles], dtype=int)
    player = np.array([p["game"]["defenderElo"] for p in puzzles], dtype=float)
    rating = np.array([p["rating"] for p in puzzles], dtype=float)
    # Maia at the player's own rating: Maia's home ground, an upper reference
    maia = np.array([logit(np.interp(e, ELOS, p["maia"])) for p, e in zip(puzzles, player)])
    print(f"{len(puzzles)} positions, found in the game: {found.mean():.1%}")
    cv = StratifiedKFold(5, shuffle=True, random_state=0)
    for name, X in [
        ("player rating only", np.c_[player]),
        ("player rating + puzzle rating", np.c_[player, rating]),
        ("player rating - puzzle rating (Elo form)", np.c_[player - rating]),
        ("Maia-2 at the player's rating", np.c_[maia]),
    ]:
        pred = cross_val_predict(LogisticRegression(), X / 400, found, cv=cv, method="predict_proba")[:, 1]
        print(f"  {name:42s} AUC {roc_auc_score(found, pred):.3f}  log loss {log_loss(found, pred):.3f}")
    print("found rate by puzzle rating:")
    for lo in range(int(rating.min()) // 200 * 200, int(rating.max()) + 1, 200):
        m = (rating >= lo) & (rating < lo + 200)
        if m.sum() >= 10:
            print(f"  {lo}-{lo + 199}: {found[m].mean():.0%} of {m.sum()} (players avg {player[m].mean():.0f})")


# ---------------------------------------------------------------- pick


def pick(cfg):
    """Ids of fair puzzles spread evenly over each rating band, one per game."""
    puzzles, _ = read_any(cfg.input)
    puzzles = [p for p in puzzles if fair(p["steps"])]
    used_games = set()
    ids = []
    for band in cfg.bands.split(","):
        lo, hi = map(int, band.split("-"))
        pool = sorted((p for p in puzzles if lo <= p["rating"] < hi), key=lambda p: p["rating"])
        # every k-th puzzle, so the band is covered from its easy end to its hard end
        k = max(1, len(pool) // cfg.per_band)
        chosen = []
        for offset in range(k):
            for p in pool[offset::k]:
                if len(chosen) < cfg.per_band and p["game"]["id"] not in used_games:
                    chosen.append(p)
                    used_games.add(p["game"]["id"])
        print(f"{band}: {len(chosen)} of {len(pool)}", file=sys.stderr)
        ids += [p["id"] for p in sorted(chosen, key=lambda p: p["rating"])]
    print(",".join(ids))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--maia-models", default="maia2_models", help="where Maia-2 weights are downloaded")
    p.add_argument("--threads", type=int, default=8, help="PyTorch threads")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("calibrate")
    c.add_argument("--puzzles", required=True, help="lichess_db_puzzle.csv.zst")
    c.add_argument("--model", required=True)
    c.add_argument("--n", type=int, default=15000)
    c.add_argument("--max-rd", type=int, default=80)
    c.add_argument("--min-plays", type=int, default=1000)
    c.add_argument("--seed", type=int, default=1)

    r = sub.add_parser("rate")
    r.add_argument("--model", required=True)
    r.add_argument("--input", required=True)
    r.add_argument("--output", required=True)

    v = sub.add_parser("validate")
    v.add_argument("--input", required=True)

    k = sub.add_parser("pick")
    k.add_argument("--input", required=True)
    k.add_argument("--bands", required=True, help="rating bands like 600-1400,1400-2000")
    k.add_argument("--per-band", type=int, default=10)

    cfg = p.parse_args()
    {"calibrate": calibrate, "rate": rate, "validate": validate, "pick": pick}[cfg.cmd](cfg)


if __name__ == "__main__":
    main()
