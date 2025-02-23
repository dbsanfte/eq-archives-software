import {
  buildAutocompleteQueryConfig,
  buildFacetConfigFromConfig,
  buildSearchOptionsFromConfig
} from "./config-helper";

export function createConfig(apiConnector) {
  return {
    searchQuery: {
      facets: buildFacetConfigFromConfig(),
      ...buildSearchOptionsFromConfig()
    },
    autocompleteQuery: buildAutocompleteQueryConfig(),
    apiConnector,
    alwaysSearchOnInitialLoad: true
  };
}