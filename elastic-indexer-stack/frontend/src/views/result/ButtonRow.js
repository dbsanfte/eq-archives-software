import React, { useState } from "react";
import PropTypes from "prop-types";
import { Button, Snackbar } from "@mui/material";
import DocumentPreview from "./DocumentPreview";

const ButtonRow = ({ result }) => {
  const [previewOpen, setPreviewOpen] = useState(false);
  const [snackbarOpen, setSnackbarOpen] = useState(false);

  const handlePreviewOpen = () => setPreviewOpen(true);
  const handlePreviewClose = () => setPreviewOpen(false);

  const handlePermalink = () => {
    // Get base URL of the application
    const baseUrl = window.location.origin;

    // Construct filter query parameters in the correct format
    const encodedId = encodeURIComponent(result.id?.raw || "");

    // Build the search URL with the filter structure
    const searchUrl = `${baseUrl}/?size=n_20_n&filters%5B0%5D%5Bfield%5D=id&filters%5B0%5D%5Bvalues%5D%5B0%5D=${encodedId}&filters%5B0%5D%5Btype%5D=all`;

    // Copy the URL to clipboard
    navigator.clipboard.writeText(searchUrl)
      .then(() => {
        setSnackbarOpen(true);
      })
      .catch(err => {
        console.error("Failed to copy URL: ", err);
        alert("Failed to copy permalink to clipboard");
      });
  };

  const handleSnackbarClose = () => {
    setSnackbarOpen(false);
  };

  return (
    <>
      <div className="archive-result-actions">
        <Button variant="outlined" onClick={handlePreviewOpen} className="archive-result-preview">
          Preview Full Text
        </Button>
        <Button
          variant="text"
          onClick={() => {
            if (result.alternate_url?.raw) {
              window.open(result.alternate_url.raw, "_blank", "noopener,noreferrer");
            }
          }}
        >
          Alternate Link
        </Button>
        <Button variant="text" onClick={handlePermalink} className="archive-result-permalink">
          Copy Permalink
        </Button>
      </div>

      <DocumentPreview open={previewOpen} onClose={handlePreviewClose} result={result} />

      {/* Notification for successful copy */}
      <Snackbar
        open={snackbarOpen}
        autoHideDuration={3000}
        onClose={handleSnackbarClose}
        message="Permalink copied to clipboard"
      />
    </>
  );
};

ButtonRow.propTypes = {
  result: PropTypes.shape({
    id: PropTypes.shape({
      raw: PropTypes.string,
    }),
    alternate_url: PropTypes.shape({
      raw: PropTypes.string,
    }),
    text_full: PropTypes.shape({
      raw: PropTypes.string,
    }),
  }).isRequired,
};

export default ButtonRow;
