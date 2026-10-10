import React from 'react';
import { render, screen, within } from '@testing-library/react';
import HeaderContent from './HeaderContent';

test('offers the MCP connection screen with an accessible icon beside the archive resources', () => {
  render(<HeaderContent />);
  const links = within(screen.getByRole('navigation', { name: 'Archive resources' }));
  expect(links.getByRole('link', { name: /Contact/ })).toBeInTheDocument();
  const mcpLink = screen.getByRole('link', { name: 'Connect with MCP' });
  expect(mcpLink).toHaveAttribute('href', '/mcp.html');
  expect(mcpLink.querySelector('img')).toHaveAttribute('src', '/images/mcp.svg');
  expect(screen.getByRole('link', { name: 'EQ Archives home' })).toHaveAttribute('href', '/');
});

test.each(['/', '/sites', '/document'])('offers clear primary navigation on %s', path => {
  window.history.replaceState({}, '', path);
  try {
    render(<HeaderContent compact />);
    const nav = within(screen.getByRole('navigation', { name: 'Archive views' }));
    expect(nav.getByRole('link', { name: 'Search' })).toHaveAttribute('href', '/');
    expect(nav.getByRole('link', { name: 'Recently indexed' })).toHaveAttribute('href', '/sites');
    if (path === '/') expect(nav.getByRole('link', { name: 'Search' })).toHaveAttribute('aria-current', 'page');
    else if (path === '/sites') expect(nav.getByRole('link', { name: 'Recently indexed' })).toHaveAttribute('aria-current', 'page');
    else expect(nav.queryByRole('link', { current: 'page' })).not.toBeInTheDocument();
  } finally { window.history.replaceState({}, '', '/'); }
});
