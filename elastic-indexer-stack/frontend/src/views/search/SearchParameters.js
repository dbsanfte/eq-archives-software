import React, { useEffect, useState } from "react";
import PropTypes from "prop-types";
import { Box, Typography, FormControlLabel, Checkbox, Tooltip } from "@mui/material";
import { embeddingService } from "../../search/EmbeddingService";

function SearchParameters({ values, onChange }) {
  const [isServiceAvailable, setIsServiceAvailable] = useState(true);
  
  useEffect(() => {
    // Check service availability initially
    setIsServiceAvailable(embeddingService.isEmbeddingServiceAvailable());
    
    // Set up interval to check service availability
    const checkInterval = setInterval(() => {
      setIsServiceAvailable(embeddingService.isEmbeddingServiceAvailable());
    }, 5000); // Check every 5 seconds
    
    return () => clearInterval(checkInterval);
  }, []);

  const handleCheckboxChange = (e) => {
    onChange("enableSemanticSearch", e.target.checked);
  };

  return (
    <Box
      p={2}
      className="archive-settings-panel"
    >
      <Typography variant="subtitle2" gutterBottom>
        Search Parameters
      </Typography>
      <Tooltip title={!isServiceAvailable ? "Embedding service is currently unavailable" : ""}>
        <FormControlLabel
          control={
            <Checkbox
              checked={isServiceAvailable && values.enableSemanticSearch}
              onChange={handleCheckboxChange}
              name="enableSemanticSearch"
              color="primary"
              disabled={!isServiceAvailable}
            />
          }
          label="Enable semantic search"
        />
      </Tooltip>
      {!isServiceAvailable && (
        <Typography variant="caption" color="error">
          Semantic search is temporarily unavailable
        </Typography>
      )}
    </Box>
  );
}

SearchParameters.propTypes = {
  values: PropTypes.shape({
    enableSemanticSearch: PropTypes.bool.isRequired,
  }).isRequired,
  onChange: PropTypes.func.isRequired,
};

export default SearchParameters;