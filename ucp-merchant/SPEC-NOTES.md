# UCP spec notes + gap analysis (refine before hardening)

Validated our skeleton against the canonical UCP spec (ucp.dev, Google+Shopify).
Sources: https://ucp.dev/ · https://ucp.dev/latest/specification/overview/ ·
https://shopify.dev/docs/agents · https://github.com/Universal-Commerce-Protocol/ucp

## Canonical `/.well-known/ucp` manifest
```json
{
  "ucp": {
    "version": "2026-04-08",
    "services":     { "<ns>": [ { "version","spec","transport","endpoint","schema" } ] },
    "capabilities": { "<cap-id>": [ { "version","spec","schema","extends?" } ] },
    "payment_handlers": { }            // optional
  },
  "signing_keys": [ { "kid","kty":"EC","crv":"P-256","x","y","use":"sig","alg":"ES256" } ]
}
```

## Standard capability IDs (reverse-domain)
`dev.ucp.shopping.catalog` (search+lookup) · `.cart` · `.checkout` · `.order`
(webhooks) · `.fulfillment` (extends checkout) · `.discount` (extends checkout) ·
`.ap2_mandate` (autonomous mandate signing) · `dev.ucp.common.identity_linking`
(OAuth 2.0). Extensions carry `extends` (string or array).

## MCP tool names by capability
- catalog: `search_catalog`, `lookup_catalog`
- cart: `create_cart`, `update_cart`
- checkout: `create_checkout`, `update_checkout`, `complete_checkout`
- order: webhook push (not client tool); identity_linking: OAuth (not a tool);
  ap2: credential signing during checkout

### MCP call/response shape
Call args wrap agent identity + a structured domain object:
```json
{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{
  "name":"create_checkout",
  "arguments":{"meta":{"ucp-agent":{"profile":"https://agent.example/profiles/x.json"}},
               "checkout":{"line_items":[...]}}}}
```
Response: `result.structuredContent` (incl. negotiated `ucp.capabilities`) **and**
`result.content[].text` (serialized JSON for backward-compat).

## Auth model
- Agent identity via `UCP-Agent: profile="<url>"` header (HTTP) or
  `meta.ucp-agent.profile` (MCP).
- HTTP Message Signatures (RFC 9421): `Signature-Input` `keyid` matched against
  the agent profile's `signing_keys[].kid` (EC P-256 / ES256).
- Credential flow **Platform → Business only**; never echo credentials.
- Capability **intersection/negotiation**: match by name → mutual highest version
  → prune orphaned extensions → repeat. (This is our IAM/L1-L3 gate, §8.3.)

## ⚠️ Our current divergences (fix on resume)
| Ours now | Canonical | Action |
|---|---|---|
| caps `catalog.search` + `catalog.lookup` | single `dev.ucp.shopping.catalog` | merge into one capability |
| cap `dev.ucp.shopping.buyer_consent` | **not a standard cap** | remove from manifest (keep HITL in checkout logic; consent = AP2 mandate / OAuth) |
| MCP tool `checkout` (one) | `create_checkout`/`update_checkout`/`complete_checkout` | split into the 3-step flow |
| MCP tool `get_product` | `lookup_catalog` | rename |
| `check_availability` | not in spec | keep as internal helper, not advertised |
| flat `arguments` | `meta.ucp-agent` + structured domain obj | adopt shape |
| flat `result` | `structuredContent` + `content[]` | adopt shape |
| caps missing `schema` URL | per-cap `schema` required | add |
| services missing `spec` | per-service `spec` required | add |
| no `signing_keys` | EC P-256 JWK required for auth | add |
| no negotiation | capability intersection algorithm | implement (= IAM tiering) |
| missing caps | `cart`, `order`, `identity_linking` | add as scope grows |

**Keep (our value-add, maps onto the standard):** server-side budget ceiling +
idempotency stay in `complete_checkout`. HITL: L2 = manual approval before
`complete_checkout`; L3 = verified `ap2_mandate` (signed) — enforced in code.

## Shopify dev validation (resume step — needs token)
1. Shopify Partners → create a **development store**.
2. Install the **Universal Commerce Agent** app (turns the store into a real UCP
   endpoint).
3. Fetch its `https://<store>/.well-known/ucp` + MCP endpoint; **diff against ours**.
4. Refine our manifest/tools/shapes to match, THEN build signing + ap2 + IAM.
