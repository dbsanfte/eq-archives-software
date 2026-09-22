import filterRegistry, {
  registerFilter,
  removeFilter,
  applyFilters,
  clearAllFilters
} from './FilterRegistry.js';

describe('FilterRegistry', () => {
  let originalWindow;
  let consoleSpy;

  beforeEach(() => {
    // Save original window for restoration
    originalWindow = global.window;
    
    // Mock window object
    global.window = {
      __GLOBAL_FILTER_REGISTRY__: undefined
    };
    
    // Mock console.log
    consoleSpy = jest.spyOn(console, 'log').mockImplementation();
    
    // Clear any existing registry
    if (global.window.__GLOBAL_FILTER_REGISTRY__) {
      global.window.__GLOBAL_FILTER_REGISTRY__.filters.clear();
    }
  });

  afterEach(() => {
    // Restore original window
    global.window = originalWindow;
    
    // Restore console
    consoleSpy.mockRestore();
    
    // Clear all filters to ensure clean state
    if (typeof clearAllFilters === 'function') {
      clearAllFilters();
    }
  });

  describe('singleton behavior', () => {
    test('should create global registry on first import', () => {
      // Re-require the module to trigger initialization
      jest.resetModules();
      require('./FilterRegistry.js');
      
      expect(global.window.__GLOBAL_FILTER_REGISTRY__).toBeDefined();
      expect(global.window.__GLOBAL_FILTER_REGISTRY__.filters).toBeInstanceOf(Map);
      expect(global.window.__GLOBAL_FILTER_REGISTRY__.debug).toBe(true);
    });

    test('should reuse existing global registry on subsequent imports', () => {
      // Set up initial registry
      global.window.__GLOBAL_FILTER_REGISTRY__ = {
        filters: new Map([['existing', () => {}]]),
        debug: false
      };
      
      jest.resetModules();
      const filterRegistryModule = require('./FilterRegistry.js');
      
      expect(filterRegistryModule.default.getFilterCount()).toBe(1);
    });
  });

  describe('registerFilter', () => {
    test('should register a new filter', () => {
      const mockFilter = jest.fn();
      
      registerFilter('test-filter', mockFilter);
      
      expect(filterRegistry.getFilterCount()).toBe(1);
      expect(consoleSpy).toHaveBeenCalledWith('[FilterRegistry] Registering filter: test-filter');
      expect(consoleSpy).toHaveBeenCalledWith('[FilterRegistry] Registry now has 1 filters');
    });

    test('should overwrite existing filter with same ID', () => {
      const mockFilter1 = jest.fn();
      const mockFilter2 = jest.fn();
      
      registerFilter('same-id', mockFilter1);
      registerFilter('same-id', mockFilter2);
      
      expect(filterRegistry.getFilterCount()).toBe(1);
    });

    test('should handle multiple unique filters', () => {
      registerFilter('filter-1', jest.fn());
      registerFilter('filter-2', jest.fn());
      registerFilter('filter-3', jest.fn());
      
      expect(filterRegistry.getFilterCount()).toBe(3);
    });

    test('should handle function with complex logic', () => {
      const complexFilter = (body) => {
        return {
          ...body,
          query: {
            bool: {
              filter: [{ term: { status: 'active' } }]
            }
          }
        };
      };
      
      registerFilter('complex-filter', complexFilter);
      
      expect(filterRegistry.getFilterCount()).toBe(1);
    });
  });

  describe('removeFilter', () => {
    test('should remove existing filter', () => {
      registerFilter('to-remove', jest.fn());
      expect(filterRegistry.getFilterCount()).toBe(1);
      
      removeFilter('to-remove');
      
      expect(filterRegistry.getFilterCount()).toBe(0);
      expect(consoleSpy).toHaveBeenCalledWith('[FilterRegistry] Removing filter: to-remove');
    });

    test('should handle removing non-existent filter gracefully', () => {
      removeFilter('non-existent');
      
      expect(filterRegistry.getFilterCount()).toBe(0);
      expect(consoleSpy).toHaveBeenCalledWith('[FilterRegistry] Removing filter: non-existent');
    });

    test('should remove only specified filter', () => {
      registerFilter('keep-this', jest.fn());
      registerFilter('remove-this', jest.fn());
      
      removeFilter('remove-this');
      
      expect(filterRegistry.getFilterCount()).toBe(1);
      expect(filterRegistry.listFilters()).toEqual(['keep-this']);
    });
  });

  describe('applyFilters', () => {
    test('should apply single filter to query body', () => {
      const addStatusFilter = (body) => ({
        ...body,
        query: {
          ...body.query,
          bool: {
            ...body.query?.bool,
            filter: [{ term: { status: 'published' } }]
          }
        }
      });
      
      registerFilter('status-filter', addStatusFilter);
      
      const inputBody = { query: { match: { title: 'test' } } };
      const result = applyFilters(inputBody);
      
      expect(result.query.bool.filter).toEqual([{ term: { status: 'published' } }]);
      expect(result.query.match).toEqual({ title: 'test' });
    });

    test('should apply multiple filters in sequence', () => {
      const filter1 = (body) => ({ ...body, filter1Applied: true });
      const filter2 = (body) => ({ ...body, filter2Applied: true });
      
      registerFilter('filter-1', filter1);
      registerFilter('filter-2', filter2);
      
      const result = applyFilters({ original: true });
      
      expect(result).toEqual({
        original: true,
        filter1Applied: true,
        filter2Applied: true
      });
    });

    test('should handle empty query body', () => {
      const addFilter = (body) => ({ ...body, filtered: true });
      registerFilter('add-filter', addFilter);
      
      const result = applyFilters({});
      
      expect(result).toEqual({ filtered: true });
    });

    test('should return unmodified body when no filters registered', () => {
      const inputBody = { query: { match_all: {} } };
      const result = applyFilters(inputBody);
      
      expect(result).toEqual(inputBody);
      expect(result).not.toBe(inputBody); // Should be a copy
    });

    test('should handle filters that modify nested structures', () => {
      const nestedFilter = (body) => ({
        ...body,
        query: {
          ...body.query,
          bool: {
            ...body.query?.bool,
            must: [
              ...(body.query?.bool?.must || []),
              { range: { date: { gte: '2023-01-01' } } }
            ]
          }
        }
      });
      
      registerFilter('date-filter', nestedFilter);
      
      const inputBody = {
        query: {
          bool: {
            must: [{ match: { title: 'test' } }]
          }
        }
      };
      
      const result = applyFilters(inputBody);
      
      expect(result.query.bool.must).toHaveLength(2);
      expect(result.query.bool.must[1]).toEqual({ range: { date: { gte: '2023-01-01' } } });
    });

    test('should log filter application when debug enabled', () => {
      registerFilter('log-test', (body) => body);
      
      applyFilters({});
      
      expect(consoleSpy).toHaveBeenCalledWith('[FilterRegistry] Applying 1 filters to query');
      expect(consoleSpy).toHaveBeenCalledWith('[FilterRegistry] - Filter ID: log-test');
      expect(consoleSpy).toHaveBeenCalledWith('[FilterRegistry] Processing filter: log-test');
    });
  });

  describe('clearAllFilters', () => {
    test('should remove all registered filters', () => {
      registerFilter('filter-1', jest.fn());
      registerFilter('filter-2', jest.fn());
      registerFilter('filter-3', jest.fn());
      
      expect(filterRegistry.getFilterCount()).toBe(3);
      
      clearAllFilters();
      
      expect(filterRegistry.getFilterCount()).toBe(0);
      expect(consoleSpy).toHaveBeenCalledWith('[FilterRegistry] Clearing all filters');
    });

    test('should handle clearing empty registry', () => {
      clearAllFilters();
      
      expect(filterRegistry.getFilterCount()).toBe(0);
    });
  });

  describe('debug functionality', () => {
    test('should enable logging', () => {
      filterRegistry.disableLogging();
      consoleSpy.mockClear();
      
      registerFilter('test', jest.fn());
      expect(consoleSpy).not.toHaveBeenCalled();
      
      filterRegistry.enableLogging();
      registerFilter('test2', jest.fn());
      
      expect(consoleSpy).toHaveBeenCalled();
    });

    test('should disable logging', () => {
      filterRegistry.enableLogging();
      filterRegistry.disableLogging();
      consoleSpy.mockClear();
      
      registerFilter('test', jest.fn());
      
      expect(consoleSpy).not.toHaveBeenCalled();
    });

    test('should list all registered filters', () => {
      // Ensure logging is enabled for this test
      filterRegistry.enableLogging();
      
      registerFilter('filter-a', jest.fn());
      registerFilter('filter-b', jest.fn());
      
      // Clear previous console calls from registration
      consoleSpy.mockClear();
      
      const filters = filterRegistry.listFilters();
      
      expect(filters).toEqual(['filter-a', 'filter-b']);
      expect(consoleSpy).toHaveBeenCalledWith('[FilterRegistry] Current filters:');
      expect(consoleSpy).toHaveBeenCalledWith('[FilterRegistry] - filter-a');
      expect(consoleSpy).toHaveBeenCalledWith('[FilterRegistry] - filter-b');
    });

    test('should return filter count', () => {
      registerFilter('count-test-1', jest.fn());
      registerFilter('count-test-2', jest.fn());
      
      expect(filterRegistry.getFilterCount()).toBe(2);
    });
  });

  describe('edge cases', () => {
    test('should handle filter function that throws error', () => {
      const errorFilter = () => {
        throw new Error('Filter error');
      };
      
      registerFilter('error-filter', errorFilter);
      
      expect(() => applyFilters({})).toThrow('Filter error');
    });

    test('should handle filter that returns null', () => {
      const nullFilter = () => null;
      registerFilter('null-filter', nullFilter);
      
      const result = applyFilters({ original: true });
      
      expect(result).toBeNull();
    });

    test('should handle filter that returns undefined', () => {
      const undefinedFilter = () => undefined;
      registerFilter('undefined-filter', undefinedFilter);
      
      const result = applyFilters({ original: true });
      
      expect(result).toBeUndefined();
    });

    test('should handle filter that modifies body in place', () => {
      const inPlaceFilter = (body) => {
        body.modified = true;
        return body;
      };
      
      registerFilter('in-place-filter', inPlaceFilter);
      
      const inputBody = { original: true };
      const result = applyFilters(inputBody);
      
      expect(result.modified).toBe(true);
      expect(result.original).toBe(true);
    });

    test('should handle registering null filter function', () => {
      expect(() => registerFilter('null-fn', null)).not.toThrow();
      expect(filterRegistry.getFilterCount()).toBe(1);
    });

    test('should handle empty string as filter ID', () => {
      registerFilter('', jest.fn());
      
      expect(filterRegistry.getFilterCount()).toBe(1);
      expect(filterRegistry.listFilters()).toEqual(['']);
    });
  });

  describe('default export', () => {
    test('should export all methods as default object', () => {
      expect(filterRegistry.registerFilter).toBe(registerFilter);
      expect(filterRegistry.removeFilter).toBe(removeFilter);
      expect(filterRegistry.applyFilters).toBe(applyFilters);
      expect(filterRegistry.clearAllFilters).toBe(clearAllFilters);
      expect(typeof filterRegistry.enableLogging).toBe('function');
      expect(typeof filterRegistry.disableLogging).toBe('function');
      expect(typeof filterRegistry.getFilterCount).toBe('function');
      expect(typeof filterRegistry.listFilters).toBe('function');
    });
  });
});
