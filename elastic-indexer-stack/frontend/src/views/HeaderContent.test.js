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
