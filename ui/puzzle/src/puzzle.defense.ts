import { attributesModule, classModule, init, type VNode } from 'snabbdom';

import menuHover from 'lib/menuHover';

import DefenseCtrl from './defense/ctrl';
import type { DefenseOpts } from './defense/interfaces';
import view from './defense/view';

const patch = init([classModule, attributesModule]);

export async function initModule(opts: DefenseOpts): Promise<void> {
  await site.asset.loadPieces;
  const element = document.querySelector('main.puzzle-defense') as HTMLElement;
  let vnode: VNode | undefined;
  const ctrl = new DefenseCtrl(opts, () => {
    if (vnode) vnode = patch(vnode, view(ctrl));
  });
  element.innerHTML = '';
  vnode = patch(element, view(ctrl));
  menuHover();
}
