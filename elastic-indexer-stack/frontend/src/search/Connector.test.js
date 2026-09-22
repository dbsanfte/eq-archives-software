import { createConnector, getSearchConfig } from './Connector';
import ElasticsearchAPIConnector from "@elastic/search-ui-elasticsearch-connector";
import { getConfig } from "../config/config-helper";
import { createConfig } from "../config/Config";
import { resolveQuery } from "./Query";
import filterRegistry from './FilterRegistry';

// Mock external dependencies
jest.mock("@elastic/search-ui-elasticsearch-connector");
jest.mock("../config/config-helper");
jest.mock("../config/Config");
jest.mock("./Query");
jest.mock("./FilterRegistry");

describe('Connector', () => {
  const mockParamsRef = { current: {} };
  const mockConfig = {
    elasticsearch_username: 'test_user',
    elasticsearch_password: 'test_pass',
    indexName: 'test_index',
    vectorFields: ['vector_field'],
    nestedVectorFields: ['nested.vector_field'],
    embeddingModel: 'test_model',
    searchFields: ['title', 'content']
  };

  beforeEach(() => {
    jest.clearAllMocks();
    getConfig.mockReturnValue(mockConfig);
    filterRegistry.getFilterCount.mockReturnValue(0);
  });

  describe('createConnector', () => {
    it('should create an ElasticsearchAPIConnector with correct configuration', () => {
      createConnector(mockParamsRef);

      expect(ElasticsearchAPIConnector).toHaveBeenCalledWith(
        {
          host: `${window.location.protocol}//${window.location.host}/elasticsearch`,
          index: 'test_index'
        },
        expect.any(Function)
      );
    });

    it('should include the knnPostProcess function', () => {
      createConnector(mockParamsRef);
      const postProcessFn = ElasticsearchAPIConnector.mock.calls[0][1];
      expect(postProcessFn).toBeInstanceOf(Function);
    });
  });

  describe('knnPostProcess', () => {
    let knnPostProcess;
    const mockRequestBody = { query: { bool: { must: [] } }, sort: [{ _score: 'desc' }] };
    const mockRequestState = {
      searchTerm: 'test query',
      sortField: 'date',
      sortDirection: 'asc'
    };

    beforeEach(() => {
      createConnector(mockParamsRef);
      knnPostProcess = ElasticsearchAPIConnector.mock.calls[0][1];
    });

    it('should handle empty search terms by applying filters', () => {
      const result = knnPostProcess(mockRequestBody, { ...mockRequestState, searchTerm: '' });

      expect(resolveQuery).not.toHaveBeenCalled();
      expect(filterRegistry.applyFilters).toHaveBeenCalledWith(mockRequestBody);
    });

    it('should call resolveQuery when search term exists', () => {
      knnPostProcess(mockRequestBody, mockRequestState);

      expect(resolveQuery).toHaveBeenCalledWith(
        mockRequestState,
        mockRequestBody,
        ['title', 'content'],
        mockParamsRef,
        ['vector_field'],
        ['nested.vector_field'],
        'test_model'
      );
    });

    it('should apply filters after query resolution', () => {
      knnPostProcess(mockRequestBody, mockRequestState);

      // Check that both functions were called
      expect(resolveQuery).toHaveBeenCalled();
      expect(filterRegistry.applyFilters).toHaveBeenCalled();

      // Check the order by comparing their invocation call order
      const resolveQueryCallOrder = resolveQuery.mock.invocationCallOrder[0];
      const applyFiltersCallOrder = filterRegistry.applyFilters.mock.invocationCallOrder[0];
      expect(resolveQueryCallOrder).toBeLessThan(applyFiltersCallOrder);
    });
  });

  describe('getSearchConfig', () => {
    it('should call createConnector and createConfig', () => {
      const mockConnector = {};
      ElasticsearchAPIConnector.mockReturnValue(mockConnector);
      createConfig.mockReturnValue({ mockConfig: true });

      const result = getSearchConfig(mockParamsRef);

      // Verify ElasticsearchAPIConnector was called (which means createConnector was executed)
      expect(ElasticsearchAPIConnector).toHaveBeenCalledWith(
        {
          host: `${window.location.protocol}//${window.location.host}/elasticsearch`,
          index: 'test_index'
        },
        expect.any(Function)
      );

      // Verify createConfig was called with the connector
      expect(createConfig).toHaveBeenCalledWith(mockConnector);

      // Verify the result is what createConfig returns
      expect(result).toEqual({ mockConfig: true });
    });
  });
});

it('groups actual connector responses by default and honors the all-captures toggle', async () => {
  const result = stamp => ({ _meta: { id: `websites/example.org/${stamp}/a` }, title: { raw: 'same page' } });
  const onSearch = jest.fn().mockResolvedValue({ results: [result('20000101000000'), result('20010101000000')], totalResults: 2 });
  ElasticsearchAPIConnector.mockReturnValueOnce({ onSearch });
  const params = { current: { groupCaptures: true } };
  const connector = createConnector(params);
  const state = { searchTerm: 'cleric', current: 1, resultsPerPage: 20 };
  expect((await connector.onSearch(state, {})).results).toHaveLength(1);
  params.current = { groupCaptures: false };
  expect((await connector.onSearch(state, {})).results).toHaveLength(2);
});

it('retains the selected sort and resolves ties by ID when browsing batches', () => {
  createConnector({ current: {} });
  const postProcess = ElasticsearchAPIConnector.mock.calls.at(-1)[1];
  filterRegistry.applyFilters.mockImplementation(body => body);
  const request = postProcess({ sort: [{ _score: 'desc' }] }, { searchTerm: '', sortField: 'capture_date', sortDirection: 'asc' });
  expect(request.sort).toEqual([{ capture_date: 'asc' }, { id: { order: 'asc', missing: '_last' } }, { _doc: 'asc' }]);
});
