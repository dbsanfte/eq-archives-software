/**
 * Service for registering and applying custom query filters.
 */

// Create a truly global singleton instance that will be shared across all imports
const globalFilterRegistryKey = '__GLOBAL_FILTER_REGISTRY__';

// Check if we already have an instance in the global namespace
if (!window[globalFilterRegistryKey]) {
  // Initialize the global instance with a filters Map
  window[globalFilterRegistryKey] = {
    filters: new Map(),
    debug: true
  };
}

// Get a reference to our global registry
const registry = window[globalFilterRegistryKey];

// Log helper function
const log = (message) => {
  if (registry.debug) {
    console.log(`[FilterRegistry] ${message}`);
  }
};

/**
 * Register a filter for a specific field
 * @param {string} id - Unique identifier for this filter
 * @param {Function} filterFn - Function that applies filter to query body
 */
export function registerFilter(id, filterFn) {
  log(`Registering filter: ${id}`);
  registry.filters.set(id, filterFn);
  log(`Registry now has ${registry.filters.size} filters`);
}

/**
 * Remove a filter by ID
 * @param {string} id - ID of filter to remove
 */
export function removeFilter(id) {
  log(`Removing filter: ${id}`);
  registry.filters.delete(id);
  log(`Registry now has ${registry.filters.size} filters`);
}

/**
 * Apply all registered filters to the query body
 * @param {Object} body - Elasticsearch query body
 * @returns {Object} - Modified query body with filters applied
 */
export function applyFilters(body) {
  log(`Applying ${registry.filters.size} filters to query`);
  registry.filters.forEach((_, id) => log(`- Filter ID: ${id}`));
  
  let modifiedBody = { ...body };
  
  registry.filters.forEach((filterFn, id) => {
    log(`Processing filter: ${id}`);
    modifiedBody = filterFn(modifiedBody);
  });
  
  return modifiedBody;
}

/**
 * Clear all registered filters
 */
export function clearAllFilters() {
  log('Clearing all filters');
  registry.filters.clear();
}

/**
 * Export all methods as default object for easier imports
 */
const filterRegistry = {
  registerFilter,
  removeFilter,
  applyFilters,
  clearAllFilters,
  
  // Debug methods
  enableLogging: () => { registry.debug = true; },
  disableLogging: () => { registry.debug = false; },
  getFilterCount: () => registry.filters.size,
  listFilters: () => {
    log('Current filters:');
    registry.filters.forEach((_, id) => log(`- ${id}`));
    return Array.from(registry.filters.keys());
  }
};

export default filterRegistry;