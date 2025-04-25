import React, { useState } from "react";
import PropTypes from "prop-types";
import { Button, Dialog, DialogTitle, DialogContent, DialogActions, Snackbar, useMediaQuery, useTheme } from "@mui/material";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

const ButtonRow = ({ result }) => {
  const [previewOpen, setPreviewOpen] = useState(false);
  const [snackbarOpen, setSnackbarOpen] = useState(false);
  const theme = useTheme();
  const isMobile = useMediaQuery(theme.breakpoints.down('sm'));

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
      <div
        style={{
          marginTop: "1rem",
          display: "flex",
          flexDirection: isMobile ? "column" : "row",
          justifyContent: "space-between",
          alignItems: isMobile ? "stretch" : "flex-start",
          gap: isMobile ? "0.75rem" : "0",
          paddingLeft: "24px",
          paddingRight: "24px"
        }}
      >
        <div style={{ 
          display: "flex", 
          flexDirection: isMobile ? "column" : "row",
          gap: isMobile ? "0.75rem" : "1rem", 
          width: isMobile ? "100%" : "auto",
          marginBottom: isMobile ? "0.75rem" : "0"
        }}>
          <Button 
            variant="outlined" 
            onClick={handlePreviewOpen}
            fullWidth={isMobile}
          >
            Preview Full Text
          </Button>
          <Button
            variant="outlined"
            onClick={() => {
              if (result.alternate_url?.raw) {
                window.open(result.alternate_url.raw, "_blank", "noopener,noreferrer");
              }
            }}
            fullWidth={isMobile}
          >
            Alternate Link
          </Button>
        </div>
        <div style={{ width: isMobile ? "100%" : "auto" }}>
          <Button 
            variant="outlined" 
            color="secondary" 
            onClick={handlePermalink}
            fullWidth={isMobile}
          >
            Copy Permalink
          </Button>
        </div>
      </div>
      
      {/* Markdown Text Preview Dialog */}
      <Dialog open={previewOpen} onClose={handlePreviewClose} fullWidth maxWidth="md">
        <DialogTitle>Preview Text</DialogTitle>
        <DialogContent dividers>
          <Markdown remarkPlugins={[remarkGfm]}>
            {/* Use the raw text if it's a string, otherwise use an empty string */}
            {typeof result.text_full?.raw === "string" ? result.text_full.raw : ""}
          </Markdown>
        </DialogContent>
        <DialogActions>
          <Button onClick={handlePreviewClose} color="primary">
            Close
          </Button>
        </DialogActions>
      </Dialog>
      
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