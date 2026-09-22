import React from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { diffLines } from 'diff';
import Comparison, { compareText, DIFF_LIMIT_MESSAGE, MAX_DIFF_LENGTH, MAX_DIFF_LINES } from './Comparison';
jest.mock('diff', () => ({ ...jest.requireActual('diff'), diffLines: jest.fn(jest.requireActual('diff').diffLines) }));
beforeEach(() => { diffLines.mockImplementation(jest.requireActual('diff').diffLines); Element.prototype.scrollIntoView = jest.fn(); });

test('shows actual added and removed source, escapes HTML and navigates changes', async () => {
  const { container } = render(<Comparison before={'Same\nOld text\n'} after={'Same\n<script>alert(1)</script>\nNew text\n'} label="extracted text" />);
  expect(await screen.findByText('2 added · 1 removed lines')).toBeVisible();
  expect(screen.getByText('Old text')).toBeVisible();
  expect(container.querySelector('.reader-added').textContent).toContain('<script>alert(1)</script>');
  expect(container.querySelector('script')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Next change' }));
  expect(document.activeElement).toHaveClass('reader-removed');
  fireEvent.click(screen.getByRole('button', { name: 'Next change' }));
  expect(document.activeElement).toHaveClass('reader-added');
  fireEvent.click(screen.getByRole('button', { name: 'Previous change' }));
  expect(document.activeElement).toHaveClass('reader-removed');
  fireEvent.click(screen.getByRole('button', { name: 'Previous change' }));
  expect(document.activeElement).toHaveClass('reader-added');
});

test('labels identical text precisely and retains whitespace and final newline differences', async () => {
  const { rerender } = render(<Comparison before="same" after="same" label="image transcription (OCR)" />);
  expect(await screen.findByText('The image transcription (OCR) is identical in these two captures.')).toBeVisible();
  rerender(<Comparison before={'same\r\n'} after={'same '} label="extracted text" />);
  expect(await screen.findByText('1 added · 1 removed lines')).toBeVisible();
  expect(screen.getByText('No newline at end of text')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: 'Previous change' }));
  expect(document.activeElement).toHaveClass('reader-added');
});

test('collapses long unchanged context while retaining every line', async () => {
  const context = Array.from({ length: 20 }, (_, i) => `Context ${i}\n`).join('');
  const { container } = render(<Comparison before={context + 'old'} after={context + 'new'} label="extracted text" />);
  expect(await screen.findByText('14 unchanged lines')).toBeVisible();
  expect(container.querySelector('details pre').textContent).toBe(Array.from({ length: 14 }, (_, i) => `Context ${i + 3}\n`).join(''));
  expect(container.querySelector('.reader-unchanged').textContent).toContain('Context 19');
});

test.each(['a'.repeat(MAX_DIFF_LENGTH + 1), '\n'.repeat(MAX_DIFF_LINES + 1)])('bounds expensive comparisons and gives access to complete source', async before => {
  const callback = jest.fn();
  compareText(before, 'small', callback);
  expect(callback).toHaveBeenCalledWith(undefined);
});

test('reports algorithm limits without claiming unchanged text', async () => {
  diffLines.mockImplementation((a, b, options) => options.callback(undefined));
  render(<Comparison before="a" after="b" label="extracted text" />);
  expect(screen.getByRole('status')).toHaveTextContent(DIFF_LIMIT_MESSAGE);
});

test('ignores an obsolete comparison finishing after a newer one', async () => {
  const callbacks = [];
  diffLines.mockImplementation((a, b, options) => { callbacks.push(options.callback); });
  const { rerender, unmount } = render(<Comparison before="old" after="older" label="extracted text" />);
  expect(screen.getByRole('status')).toHaveTextContent('Comparing');
  rerender(<Comparison before="new" after="new" label="extracted text" />);
  act(() => callbacks[1]([{ value: 'new', count: 1 }]));
  expect(screen.getByRole('status')).toHaveTextContent('identical');
  act(() => callbacks[0](undefined));
  expect(screen.getByRole('status')).toHaveTextContent('identical');
  unmount();
  act(() => callbacks[1](undefined));
});
