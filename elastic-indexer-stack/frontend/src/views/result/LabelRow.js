import React from "react";
import PropTypes from "prop-types";
import { Chip, Box } from "@mui/material";

const LabelRow = ({ flavour, mailingList, domain, captureDate, guessedDate }) => (
  <Box className="archive-result-labels">
    {flavour && String(flavour).trim() !== "" && (
      <Chip label={`${flavour}`} title="Suggested content type" />
    )}
    {mailingList && String(mailingList).trim() !== "" && (
      <Chip label={`${mailingList}`} title="Yahoo mailing list" />
    )}
    {domain && domain.trim() !== "" && (
      <Chip label={`${domain}`} title="Source domain" />
    )}
    {captureDate && captureDate.trim() !== "" && (
      <Chip label={`Captured: ${captureDate}`} title="Date this page was captured" />
    )}
    {guessedDate && String(guessedDate).trim() !== "" && (
      <Chip
        className="archive-result-label--estimated"
        label={`Estimated: ${guessedDate}`}
        title="Estimated content date, inferred by AI; may be inaccurate"
      />
    )}
  </Box>
);

LabelRow.propTypes = {
  flavour: PropTypes.any,
  mailingList: PropTypes.any,
  domain: PropTypes.string,
  captureDate: PropTypes.string,
  guessedDate: PropTypes.any
};

export default LabelRow;