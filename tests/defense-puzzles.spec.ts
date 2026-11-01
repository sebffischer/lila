import { test, expect, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const puzzles: { solution: string; refutations: Record<string, unknown> }[] = JSON.parse(
  readFileSync(join(__dirname, '../modules/puzzle/src/main/resources/defensePuzzles.json'), 'utf8'),
);

async function clickSquare(page: Page, key: string) {
  const board = page.locator('cg-board');
  const box = (await board.boundingBox())!;
  const white = await page.locator('.cg-wrap.orientation-white').count();
  const file = key.charCodeAt(0) - 'a'.charCodeAt(0);
  const rank = Number(key[1]) - 1;
  const size = box.width / 8;
  const x = (white ? file : 7 - file) + 0.5;
  const y = (white ? 7 - rank : rank) + 0.5;
  await page.mouse.click(box.x + x * size, box.y + y * size);
}

async function play(page: Page, uci: string) {
  await clickSquare(page, uci.slice(0, 2));
  await clickSquare(page, uci.slice(2, 4));
}

test('a wrong move is refuted, the only move that holds is accepted', async ({ page }) => {
  const puzzle = puzzles[0];
  const wrong = Object.keys(puzzle.refutations)[0];
  await page.goto('/training/defense#1');
  const feedback = page.locator('.puzzle-defense__feedback');
  await expect(feedback).toContainText("Find the only move that doesn't lose material.");
  await expect(page.getByRole('button', { name: 'View the solution' })).toBeVisible();
  // the threat button turns into the retry button: its click handler must not stick around
  await page.getByRole('button', { name: 'Show the threat' }).click();

  await play(page, wrong);
  await expect(feedback).toHaveClass(/fail/);
  await expect(feedback).toContainText(/This (loses material|allows checkmate)/);

  await page.getByRole('button', { name: 'Retry' }).click();
  await play(page, puzzle.solution);
  await expect(feedback).toHaveClass(/good/);
  await expect(feedback).toContainText('The only move that holds!');

  // the first try failed, so the puzzle counts as failed
  await expect(page.locator('.puzzle__session a').first()).toHaveClass(/result-false/);
  await page.getByRole('button', { name: 'Next puzzle' }).click();
  await expect(page).toHaveURL(/#2$/);
});

test('the threat can be shown as a hint', async ({ page }) => {
  await page.goto('/training/defense#1');
  await page.getByRole('button', { name: 'Show the threat' }).click();
  await expect(page.locator('.puzzle-defense__feedback')).toContainText('Your opponent threatens');
  await expect(page.locator('svg.cg-shapes line').first()).toBeAttached();
});

test('after a wrong move, the solution can be viewed and the next puzzle loaded', async ({ page }) => {
  const puzzle = puzzles[2];
  await page.goto('/training/defense#3');
  await expect(page.getByRole('button', { name: 'View the solution' })).toBeVisible();
  await play(page, Object.keys(puzzle.refutations)[0]);
  await page.getByRole('button', { name: 'View the solution' }).click();
  await expect(page.locator('.puzzle-defense__feedback')).toHaveClass(/solution/);
  await page.getByRole('button', { name: 'Next puzzle' }).click();
  await expect(page).toHaveURL(/#4$/);
  await expect(page.locator('.puzzle-defense__title')).toContainText('4 / 10');
});
