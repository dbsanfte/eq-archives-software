import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import App from './App';
import { getSearchConfig } from './search/Connector';

let mockSearchState;
jest.mock('./search/Connector', () => ({ getSearchConfig: jest.fn() }));
jest.mock('./config/config-helper', () => ({
  getConfig: () => ({ titleField: 'title', urlField: 'url', thumbnailField: 'thumbnail' }),
  getStandardFacetFields: () => ['domain'],
  getDatePickerFacetFields: () => ['capture_date'],
  buildSortOptionsFromConfig: () => []
}));
jest.mock('@elastic/react-search-ui', () => ({
  SearchProvider: ({ children }) => children,
  WithSearch: ({ children, mapContextToProps }) => children(mapContextToProps(mockSearchState)),
  ErrorBoundary: ({ children }) => children,
  Facet: ({ label }) => <span>{label}</span>,
  Results: () => <span>Search results</span>,
  PagingInfo: () => <span>Page information</span>,
  ResultsPerPage: () => <span>Page size</span>,
  Paging: () => <span>Paging</span>,
  Sorting: () => <span>Sort results</span>
}));
jest.mock('@elastic/react-search-ui-views', () => ({
  Layout: props => <main>{Object.values(props)}</main>
}));
jest.mock('./views/ArchiveStatusBar', () => () => <span>Archive status</span>);
jest.mock('./views/HeaderContent', () => () => <h1>Search archives</h1>);
jest.mock('./views/result/CustomResultView', () => () => null);
jest.mock('./views/search/EnhancedSearchBox', () => () => <input aria-label="Search archives" />);
jest.mock('./views/search/SyntaxExamples', () => () => <span>Query syntax help</span>);
jest.mock('./views/search/DateRangeFacet', () => ({ label }) => <span>{label}</span>);
jest.mock('./views/search/SearchParameters', () => ({ onChange }) =>
  <button onClick={() => onChange('enableSemanticSearch', false)}>Disable semantic search</button>
);
jest.mock('./views/search/AdvancedSettings', () => ({
  __esModule: true,
  DEFAULT_KNN_PARAMS: { enableSemanticSearch: true, k: 10, num_candidates: 100, boost: 5 },
  default: ({ values, onChange }) => <button onClick={() => onChange('k', 25)}>Neighbors: {values.k}</button>
}));

beforeEach(() => {
  jest.clearAllMocks();
  getSearchConfig.mockReturnValue({ searchQuery: { facets: {} } });
  mockSearchState = { wasSearched: true, isLoading: true, executeSearch: jest.fn() };
  jest.spyOn(window, 'scrollTo').mockImplementation(() => {});
});
afterEach(() => jest.restoreAllMocks());

test('exposes search results, date facets, advanced options, syntax help and scroll-to-top', () => {
  render(<App />);
  expect(screen.getByRole('progressbar')).toBeInTheDocument();
  expect(screen.getByText('Sort results')).toBeInTheDocument();
  expect(screen.getByText('capture_date')).toBeInTheDocument();
  expect(screen.getByText('domain')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Advanced...' }));
  fireEvent.click(screen.getByRole('button', { name: 'Neighbors: 10' }));
  expect(screen.getByRole('button', { name: 'Neighbors: 25' })).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Disable semantic search' }));
  expect(getSearchConfig.mock.calls[0][0].current.enableSemanticSearch).toBe(false);
  fireEvent.click(screen.getByRole('button', { name: 'Search Syntax...' }));
  expect(screen.getByText('Query syntax help')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: 'Back to Top' }));
  expect(window.scrollTo).toHaveBeenCalledWith({ top: 0, behavior: 'smooth' });
});

test('renders before the first search even without optional search configuration', () => {
  getSearchConfig.mockReturnValue({});
  mockSearchState = { wasSearched: false, isLoading: false };
  render(<App />);
  expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
  expect(screen.queryByText('Sort results')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Back to Top' })).not.toBeInTheDocument();
});
