import React from "react";
import { Box, Typography, Link } from "@mui/material";

export default function HeaderContent() {
  return (
    <Box 
      display="flex" 
      flexDirection={{ xs: "column", md: "row" }}
      alignItems="center" 
      gap={2}
      justifyContent={{ xs: "center", md: "space-between" }}
      p={2} 
      mb={2}
      bgcolor="#a69070" // Match logo background
    >
      <Box textAlign={{ xs: "center", md: "left" }}>
        <a href="/">
          <img 
            src="/images/eq-logo-no-bg.webp" 
            alt="EQ Logo" 
            style={{ 
              maxHeight: "170px",
              maxWidth: "100%",
              height: "auto"
            }}
          />
        </a>
      </Box>
      
      <Box textAlign="center">
        <img 
          src="/images/eq-archives-banner.png" 
          alt="EQ Archives Banner"
          style={{ 
            maxHeight: "170px",
            maxWidth: "100%",
            height: "auto"
          }}
        />
      </Box>
      
      <Box textAlign={{ xs: "center", md: "right" }}>
        <Typography variant="h6">
          Welcome to the new search portal!
        </Typography>
        <Link 
          href="https://www.youtube.com/watch?v=DWXsCpAwKU4" 
          target="_blank" 
          rel="noopener noreferrer"
          variant="subtitle1"
          style={{ display: "block", color: "#ffffff" }}
        >
          Watch the Tutorial
        </Link>
        <Link 
          href="https://discord.com/channels/@me/312315372021743616" 
          target="_blank" 
          rel="noopener noreferrer"
          variant="subtitle2"
          style={{ display: "block", color: "#ffffff" }}
        >
          Contact Dolalin on P99 Discord
        </Link>
      </Box>
    </Box>
  );
}