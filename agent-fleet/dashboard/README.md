# React + TypeScript + Vite

This template provides a minimal setup to get React working in Vite with HMR and some Oxlint rules.

Currently, two official plugins are available:

- [@vitejs/plugin-react](https://github.com/vitejs/vite-plugin-react/blob/main/packages/plugin-react) uses [Oxc](https://oxc.rs)
- [@vitejs/plugin-react-swc](https://github.com/vitejs/vite-plugin-react/blob/main/packages/plugin-react-swc) uses [SWC](https://swc.rs/)

## React Compiler

The React Compiler is not enabled on this template because of its impact on dev & build performances. To add it, see [this documentation](https://react.dev/learn/react-compiler/installation).

## Expanding the Oxlint configuration

If you are developing a production application, we recommend enabling type-aware lint rules by installing `oxlint-tsgolint` and editing `.oxlintrc.json`:

```json
{
  "$schema": "./node_modules/oxlint/configuration_schema.json",
  "plugins": ["react", "typescript", "oxc"],
  "options": {
    "typeAware": true
  },
  "rules": {
    "react/rules-of-hooks": "error",
    "react/only-export-components": ["warn", { "allowConstantExport": true }]
  }
}
```

See the [Oxlint rules documentation](https://oxc.rs/docs/guide/usage/linter/rules) for the full list of rules and categories.

## Research workflow integration (7 September 2026)

The active routes `/` and `/ask` use the existing POST `/search` and POST `/ask`
contracts. Search is lexical by default, with the existing context expansion flags;
it does not advertise semantic retrieval. Submit explicitly (including a query
prefilled by a URL). Ask can invoke generation on the connected backend when used
manually; automated tests never invoke a model.

Search results open article details from the returned search snapshot, including
Arabic/English text, chapter, IDs, score, tier and reported status. No document
lookup endpoint exists: a direct detail link without its search state shows an
unavailable message. Source URLs, provenance hashes and version history are not
invented. Citation disclosures show resolution/coverage, matched ID, optional
snippet and validation error. They cannot fetch full text from a citation ID.
Unknown tier/status/translation remain unknown. Trust values are backend signals,
not guarantees of currency; translation exposure is not translation accuracy.

`/dashboard`, `/browse`, and `/source/:sourceId` explicitly show unavailable
features. The previous BrowsePage and DashboardPage mock implementations remain
on disk to preserve prototype work, but are not imported or mounted by App.
Client records, catalogue counts, exports and history are not backend features.

### Run in a browser

From `/home/ais04/sanad/ui-prototype`:

```bash
npm install
VITE_API_BASE=/api SANAD_API_PROXY_TARGET=http://127.0.0.1:8000 npm run dev -- --host 127.0.0.1
```

Open the local URL printed by Vite (normally http://127.0.0.1:5173).
Start/configure the API separately. `/api/search` and `/api/ask` are forwarded to
`/search` and `/ask` by Vite. Explicit environment values above override any
existing `.env.local` (existing local overrides were preserved). No DB setup is
needed for the UI test suite.

The API's optional `SANAD_API_KEY` is a server-side setting, disabled by default
locally. When enabled, open **API access**, enter the key and **Apply key**. It is
sent as `Authorization: Bearer …` and held only in module memory, shared across
routes in this tab. **Clear key** or reload removes it. The input clears after
application. No localStorage/sessionStorage, URL, build-time key or key logging.
Previously rendered research results are not removed when a key is cleared.

### Production

```bash
VITE_API_BASE=/api npm run build
npm run preview -- --host 127.0.0.1
```

Vite preview serves the built UI; it is not a production API reverse proxy.
Configure your production host to proxy `/api/*` to the API with `/api` stripped,
forward Authorization, and serve `index.html` for SPA navigation paths. Alternatively
build with `VITE_API_BASE=https://your-api.example` (or a URL with a path prefix)
and configure that API/gateway's CORS for the frontend origin and Authorization.
This variable is public and fixed at build time. Never place a secret in it.

### Verification and dependencies

```bash
npm test
npm run lint
npm run typecheck
npm run build
```

New development dependencies: `vitest`, `jsdom`, `@testing-library/react`,
`@testing-library/user-event`, `@testing-library/jest-dom`, `msw`. No new runtime
dependencies. MSW intercepts HTTP; unhandled requests fail rather than reaching a
backend. Test configuration fixes the API base to `/api` independent of developer
`.env.local` overrides. Workflow tests cover request bodies, loading, source
navigation, HTML escaping, unknown metadata, empty results, error/recovery,
abstention, citation disclosures, optional Bearer auth and unsupported routes.
These are jsdom interaction tests, not a real browser or live service verification.
