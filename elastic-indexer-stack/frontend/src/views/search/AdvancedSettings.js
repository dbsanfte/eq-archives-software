import React, { useState, useEffect } from "react";
import PropTypes from "prop-types";
import { Box, Typography, TextField } from "@mui/material";

// Define default parameters here
export const DEFAULT_KNN_PARAMS = {
  enableSemanticSearch: true,
  k: 10,
  num_candidates: 100,
  boost: 5,
};

export default function AdvancedSettings({ onChange, values }) {
  // Keep local copies so typing doesn't cause the parent to re-render immediately
  const [localValues, setLocalValues] = useState(values);

  // Sync localValues if parent values change from elsewhere
  useEffect(() => {
    setLocalValues(values);
  }, [values]);

  const handleFieldChange = (field, val) => {
    setLocalValues((prev) => ({ ...prev, [field]: val }));
  };

  // Only commit changes to parent onBlur
  const handleFieldBlur = (field, val) => {
    onChange(field, val);
  };

  return (
    <Box p={2} className="archive-settings-panel">
      <Typography variant="subtitle2" gutterBottom>
        Vector Search Parameters
      </Typography>
      <Box className="archive-settings-fields">
        <TextField
          label="k"
          variant="outlined"
          size="small"
          value={localValues.k}
          onChange={(e) => handleFieldChange("k", e.target.value)}
          onBlur={(e) => handleFieldBlur("k", e.target.value)}
          disabled={!values.enableSemanticSearch}
        />
        <TextField
          label="num_candidates"
          variant="outlined"
          size="small"
          value={localValues.num_candidates}
          onChange={(e) => handleFieldChange("num_candidates", e.target.value)}
          onBlur={(e) => handleFieldBlur("num_candidates", e.target.value)}
          disabled={!values.enableSemanticSearch}
        />
        <TextField
          label="boost"
          variant="outlined"
          size="small"
          value={localValues.boost}
          onChange={(e) => handleFieldChange("boost", e.target.value)}
          onBlur={(e) => handleFieldBlur("boost", e.target.value)}
          disabled={!values.enableSemanticSearch}
        />
      </Box>
    </Box>
  );
}

AdvancedSettings.propTypes = {
  onChange: PropTypes.func.isRequired,
  values: PropTypes.shape({
    enableSemanticSearch: PropTypes.bool.isRequired,
    k: PropTypes.oneOfType([PropTypes.number, PropTypes.string]).isRequired,
    num_candidates: PropTypes.oneOfType([PropTypes.number, PropTypes.string]).isRequired,
    boost: PropTypes.oneOfType([PropTypes.number, PropTypes.string]).isRequired,
  }).isRequired,
};