import React from 'react';
import { render, screen, within } from '@testing-library/react';
import HeaderContent from './HeaderContent';

test('offers the ChatGPT connection guide alongside the archive resources', () => {
  render(<HeaderContent />);
  const links = within(screen.getByRole('navigation', { name: 'Archive resources' }));
  expect(links.getByRole('link', { name: 'ChatGPT' })).toHaveAttribute('href', '/chatgpt.html');
  expect(screen.getByRole('link', { name: 'EQ Archives home' })).toHaveAttribute('href', '/');
});
