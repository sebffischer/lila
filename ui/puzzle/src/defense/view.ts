import { Chessground as makeChessground } from '@lichess-org/chessground';
import { h, type VNode } from 'snabbdom';

import { pubsub } from 'lib/pubsub';
import { makeConfig } from 'lib/puz/view/chessground';
import { bind, onInsert, type MaybeVNode } from 'lib/view';

import type DefenseCtrl from './ctrl';
import type { DefenseLine } from './interfaces';
import { passPosition, sanLine } from './logic';

export default function view(ctrl: DefenseCtrl): VNode {
  return h('main.puzzle-defense', [
    h('div.puzzle-defense__board.main-board', [chessground(ctrl), ctrl.promotion.view()]),
    h('div.puzzle-defense__side', [
      h('div.puzzle-defense__title', [
        h('h1', i18n.puzzle.defensePuzzles),
        h('span', `${ctrl.index + 1} / ${ctrl.puzzles.length}`),
      ]),
      feedback(ctrl),
      actions(ctrl),
      session(ctrl),
      source(ctrl),
    ]),
  ]);
}

const chessground = (ctrl: DefenseCtrl): VNode =>
  h('div.cg-wrap', {
    hook: onInsert(el => {
      ctrl.ground(makeChessground(el, makeConfig(ctrl.cgConfig(), ctrl.pref, ctrl.userMove)));
      pubsub.on('board.change', (is3d: boolean) =>
        ctrl.withGround(g => {
          g.state.addPieceZIndex = is3d;
          g.redrawAll();
        }),
      );
    }),
  });

const feedbackBox = (cls: string, icon: VNode, title: string, text?: MaybeVNode): VNode =>
  h(`div.puzzle-defense__feedback.${cls}`, [
    h('div.player', [icon, h('div.instruction', [h('strong', title), text ? h('em', text) : null])]),
  ]);

const pieceIcon = (color: Color) => h('div.no-square', h(`piece.king.${color}`));
const textIcon = (text: string) => h('div.icon', text);

function feedback(ctrl: DefenseCtrl): VNode {
  const p = ctrl.puzzle;
  switch (ctrl.mode) {
    case 'intro':
    case 'play':
      return feedbackBox(
        'play',
        pieceIcon(ctrl.pov),
        i18n.site.yourTurn,
        ctrl.threatShown && p.threat
          ? i18n.puzzle.opponentThreatensX(threatSan(ctrl, p.threat))
          : i18n.puzzle.findTheOnlyMoveThatHolds,
      );
    case 'fail': {
      const wrong = ctrl.wrong!;
      const line = sanLine(ctrl.pos, [wrong.uci, ...(wrong.refutation?.line || [])], '?');
      return feedbackBox(
        'fail',
        textIcon('✗'),
        i18n.puzzle.notTheMove,
        !wrong.refutation
          ? i18n.puzzle.trySomethingElse
          : wrong.refutation.mate
            ? i18n.puzzle.thisAllowsMateX(line)
            : i18n.puzzle.thisLosesMaterialX(line),
      );
    }
  }
  // good or solution
  return feedbackBox(
    ctrl.mode,
    textIcon('✓'),
    ctrl.mode === 'good' ? i18n.puzzle.onlyMoveThatHolds : sanLine(ctrl.pos, [p.solution], '!'),
    p.threat ? i18n.puzzle.itParriesX(threatSan(ctrl, p.threat)) : undefined,
  );
}

const threatSan = (ctrl: DefenseCtrl, threat: DefenseLine): string => {
  const pass = passPosition(ctrl.pos);
  return pass ? sanLine(pass, threat.line) : '';
};

// The key stops snabbdom from reusing one button's element, and its click handler, for another.
const button = (text: string, action: () => void, primary = false): VNode =>
  h(`button.button${primary ? '' : '.button-empty'}`, { key: text, hook: bind('click', action) }, text);

function actions(ctrl: DefenseCtrl): VNode {
  const buttons: MaybeVNode[] = [];
  switch (ctrl.mode) {
    case 'play':
      if (ctrl.puzzle.threat)
        buttons.push(button(i18n.puzzle.showTheThreat, ctrl.toggleThreat, ctrl.threatShown));
      buttons.push(button(i18n.site.viewTheSolution, ctrl.viewSolution));
      break;
    case 'fail':
      buttons.push(
        button(i18n.site.retry, ctrl.retry, true),
        button(i18n.site.viewTheSolution, ctrl.viewSolution),
      );
      break;
    case 'good':
    case 'solution':
      buttons.push(button(i18n.puzzle.nextPuzzle, ctrl.next, true));
  }
  return h('div.puzzle-defense__actions', buttons);
}

function session(ctrl: DefenseCtrl): VNode {
  const results = ctrl.results();
  return h('div.puzzle-defense__session', [
    h(
      'div.puzzle__session',
      ctrl.puzzles.map((p, i) =>
        h(
          'a',
          {
            class: {
              current: i === ctrl.index,
              'result-true': results[p.id] === true,
              'result-false': results[p.id] === false,
            },
            hook: bind('click', () => ctrl.load(i)),
          },
          `${i + 1}`,
        ),
      ),
    ),
    h('div.puzzle-defense__score', [
      i18n.puzzle.heldOnFirstTryX(`${ctrl.firstTryWins()} / ${ctrl.puzzles.length}`),
      Object.keys(results).length
        ? h(
            'button.button.button-empty.button-thin',
            { hook: bind('click', ctrl.resetResults) },
            i18n.site.reset,
          )
        : null,
    ]),
  ]);
}

function source(ctrl: DefenseCtrl): VNode {
  const { game } = ctrl.puzzle;
  const done = ctrl.mode === 'good' || ctrl.mode === 'solution' || ctrl.mode === 'fail';
  const fen = ctrl.puzzle.fen.replace(/ /g, '_');
  return h('div.puzzle-defense__source', [
    h(
      'span',
      i18n.puzzle.fromGameLink.asArray(
        h('a', { attrs: { href: `https://lichess.org/${game.id}#${game.ply}`, target: '_blank' } }, [
          `${game.white} - ${game.black}`,
        ]),
      ),
    ),
    // the analysis board would give the answer away, so only link it once the puzzle is over
    done
      ? h('a', { attrs: { href: `/analysis/standard/${fen}?color=${ctrl.pov}` } }, i18n.site.analysis)
      : null,
  ]);
}
