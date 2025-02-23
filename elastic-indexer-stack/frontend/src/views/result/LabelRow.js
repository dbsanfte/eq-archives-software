import React from "react";
import PropTypes from "prop-types";
import { Chip, Box } from "@mui/material";

const LabelRow = ({ flavour, mailingList, domain, captureDate, guessedDate }) => {
  return (
    <Box
      sx={{
        marginTop: "1rem",
        display: "flex",
        gap: "1rem",
        flexWrap: "wrap",
        paddingLeft: "24px"
      }}
    >
      {flavour && String(flavour).trim() !== "" && (
        <Chip
          label={`${flavour}`}
          sx={{ backgroundColor: "#9c27b0", color: "#fff" }}
          tooltip={"LLM Content Flavour"}
        />
      )}
      {mailingList && String(mailingList).trim() !== "" && (
        <Chip
          label={`${mailingList}`}
          sx={{ backgroundColor: "#f44336", color: "#fff" }}
          tooltip={"Yahoo Mailing List Name"}
        />
      )}
      {domain && domain.trim() !== "" && (
        <Chip
          label={`${domain}`}
          sx={{ backgroundColor: "#2196f3", color: "#fff" }}
          tooltip={"Domain Name"}
        />
      )}
      {captureDate && captureDate.trim() !== "" && (
        <Chip
          label={`Capture Date: ${captureDate}`}
          sx={{ backgroundColor: "#4caf50", color: "#fff" }}
          tooltip={"Date of Capture (reliable)"}
        />
      )}
      {guessedDate && String(guessedDate).trim() !== "" && (
        <Chip
          label={`Guessed Date: ${guessedDate}`}
          sx={{ backgroundColor: "#ff9800", color: "#fff" }}
          tooltip={"Guessed Content Date from LLM (unreliable)"}
        />
      )}
    </Box>
  );
};

LabelRow.propTypes = {
  flavour: PropTypes.any,
  mailingList: PropTypes.any,
  domain: PropTypes.string,
  captureDate: PropTypes.string,
  guessedDate: PropTypes.any
};

export default LabelRow;