# Landing page (Netlify)

A self-contained static page for the project. No build step, no dependencies,
just one HTML file.

## Deploy in two steps

1. Open `index.html` and replace the `CONNECTOR_URL` constant at the top of
   the `<script>` block with your real MCP endpoint from the Render deploy,
   for example:

   ```js
   const CONNECTOR_URL = "https://your-app.onrender.com/mcp";
   ```

   The URL lives in that one constant only; the page and the copy button both
   read from it.

2. Drag this `landing/` folder onto [netlify.com](https://app.netlify.com/drop).
   Done.
