import React from 'react';
import { render, screen } from '@testing-library/react';
import App from './App';

test('renders without crashing', () => {
  render(<App />);
  expect(screen.getByTestId('app-container')).toBeInTheDocument();
});