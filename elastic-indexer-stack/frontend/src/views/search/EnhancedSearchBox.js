import React, { useCallback, useEffect, useMemo, useRef } from "react";
import { SearchBox, withSearch } from "@elastic/react-search-ui";
import { embeddingService } from "../../search/EmbeddingService";
import { getConfig } from "../../config/config-helper";
import debounce from "lodash/debounce";
import PropTypes from "prop-types";

const EnhancedSearchBox = ({
  searchTerm,
  setSearchTerm,
  searchAsYouType,
  autocompleteSuggestions,
  ...props
}) => {
  const { embeddingModel } = getConfig();
  const revision = useRef(0);
  const latestValue = useRef(searchTerm);

  const executeSearchWithEmbedding = useCallback(async (value, requestRevision) => {
    try {
      if (value.trim() && !embeddingService.getEmbedding(value)) {
        await embeddingService.fetchEmbedding(value, embeddingModel);
      }
    } catch (error) {
      console.error("Error fetching embedding:", error);
      // The current query can still fall back to text search.
    }

    // Typing, submitting, navigating, or unmounting invalidates older work.
    if (requestRevision === revision.current) {
      setSearchTerm(value);
    }
  }, [embeddingModel, setSearchTerm]);

  const debouncedSearch = useMemo(
    () => debounce(executeSearchWithEmbedding, 400),
    [executeSearchWithEmbedding]
  );

  useEffect(() => () => {
    debouncedSearch.cancel();
    revision.current += 1;
  }, [debouncedSearch]);

  useEffect(() => {
    // Honor changes made outside the input, such as browser history navigation.
    if (searchTerm !== latestValue.current) {
      latestValue.current = searchTerm;
      revision.current += 1;
      debouncedSearch.cancel();
    }
  }, [searchTerm, debouncedSearch]);

  const updateInput = (value) => {
    debouncedSearch.cancel();
    const requestRevision = ++revision.current;
    latestValue.current = value;
    // Search UI owns the input value. Update it immediately, but defer the
    // network search until the debounce and embedding preparation complete.
    setSearchTerm(value, { refresh: false });
    return requestRevision;
  };

  return (
    <SearchBox
      searchTerm={searchTerm || ""}
      onChange={(value) => {
        const requestRevision = updateInput(value);
        if (searchAsYouType) debouncedSearch(value, requestRevision);
      }}
      onSubmit={(value) => {
        const requestRevision = updateInput(value);
        return executeSearchWithEmbedding(value, requestRevision);
      }}
      autocompleteSuggestions={autocompleteSuggestions}
      {...props}
    />
  );
};

EnhancedSearchBox.propTypes = {
  searchTerm: PropTypes.string,
  setSearchTerm: PropTypes.func.isRequired,
  searchAsYouType: PropTypes.bool,
  autocompleteSuggestions: PropTypes.oneOfType([
    PropTypes.bool,
    PropTypes.object
  ])
};

export default withSearch(({
  searchTerm,
  setSearchTerm,
  searchAsYouType,
  autocompleteSuggestions
}) => ({
  searchTerm,
  setSearchTerm,
  searchAsYouType,
  autocompleteSuggestions
}))(EnhancedSearchBox);
