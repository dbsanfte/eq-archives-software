const copyButton = document.getElementById('copy-endpoint');
const copyStatus = document.getElementById('copy-status');
copyButton.hidden = false;
copyButton.addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(document.getElementById('mcp-endpoint').textContent);
    copyStatus.textContent = 'MCP server URL copied.';
  } catch {
    copyStatus.textContent = 'Select and copy the server URL above.';
  }
});
