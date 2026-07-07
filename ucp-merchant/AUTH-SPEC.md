# UCP Merchant Authentication & Checkout Implementation Reference
## Comprehensive Technical Specification for Building Auth + Checkout Layer

**Source Documentation:** https://ucp.dev/ | https://github.com/Universal-Commerce-Protocol/ucp  
**Last Updated:** 2026-06-29 | **API Version:** 2026-04-08

> ⚠️ **ACCURACY CAVEAT (read first).** This was machine-extracted from ucp.dev and
> has KNOWN internal inconsistencies in exact wire shapes — do NOT code manifest/
> signature structures verbatim from it. Verify against the canonical **JSON
> Schemas** + the **GitHub repo** (github.com/Universal-Commerce-Protocol/ucp,
> /samples, /conformance) first. Known conflicts vs. the directly-fetched spec:
> - `services`: here as `{ns:{rest:{},mcp:{}}}` BUT the spec overview shows
>   `{ns:[{version,spec,transport,endpoint,schema}]}` (array w/ transport field).
> - `capabilities`: here as `{id:{...}}` BUT the spec overview shows
>   `{id:[{version,spec,schema,extends}]}` (ARRAY of versions). Our merchant uses
>   the array form (correct).
> The CONCEPTS (RFC 9421 sigs, AP2 SD-JWT mandates, RFC 8785 JCS, OAuth PKCE,
> checkout lifecycle, totals contract, credential-flow direction) are sound.

---

## 1. Manifest (`.well-known/ucp`)

**Specification Source:** https://ucp.dev/specification/overview  
**Profile Discovery:** https://ucp.dev/latest/specification/checkout-rest/

The manifest is a JSON object published at `/.well-known/ucp` by the merchant. It declares protocol capabilities, transports, payment handlers, and signing keys for discovery and cryptographic verification.

### Top-Level Structure

```json
{
  "ucp": {
    "version": "2026-04-08",
    "services": { /* service declarations */ },
    "capabilities": { /* capability declarations */ },
    "payment_handlers": { /* payment handler specs */ }
  },
  "signing_keys": [ /* JWK public keys */ ],
  "supported_versions": { /* optional: legacy version mappings */ }
}
```

**Field Definitions:**

- **`ucp.version`** (string, required): Date-based protocol version (format: `YYYY-MM-DD`). Identifies the version of UCP the merchant implements.
- **`ucp.services`** (object, required): Maps service names to transport bindings.
- **`ucp.capabilities`** (object, required): Maps capability names to capability declarations.
- **`ucp.payment_handlers`** (object, optional): Maps payment handler IDs to handler specifications.
- **`signing_keys`** (array[JWK], required): Public keys for HTTP Message Signature verification (RFC 9421).
- **`supported_versions`** (object, optional): Maps older versions to alternative profile URIs for backward compatibility.

### Services Object

Services are vertical-specific operation groups (e.g., `dev.ucp.shopping`). Each service declares available transports.

```json
{
  "ucp": {
    "services": {
      "dev.ucp.shopping": {
        "rest": {
          "endpoint": "https://merchant.example/ucp/checkout",
          "schema": "https://merchant.example/schemas/shopping-rest.json"
        },
        "mcp": {
          "endpoint": "https://merchant.example/mcp",
          "schema": "https://merchant.example/schemas/shopping-mcp.json"
        }
      }
    }
  }
}
```

**Transport Options:**
- `rest`: REST/HTTP endpoint URL + OpenAPI 3.x schema URL
- `mcp`: Model Context Protocol endpoint URL + OpenRPC schema URL
- `a2a`: Agent-to-Agent Card endpoint + schema URL
- `embedded`: iframe/webview endpoint + schema URL

**Required fields per transport:** `endpoint` (absolute HTTPS URL), `schema` (absolute URL to spec document)

### Capabilities Object

Each capability is independently versioned and declares supported features.

```json
{
  "ucp": {
    "capabilities": {
      "dev.ucp.shopping.checkout": {
        "version": "2026-04-08",
        "spec": "https://ucp.dev/specification/checkout",
        "schema": "https://ucp.dev/schemas/checkout-2026-04-08.json",
        "extends": null
      },
      "dev.ucp.shopping.ap2_mandate": {
        "version": "2026-04-08",
        "spec": "https://ucp.dev/specification/ap2-mandates",
        "schema": "https://ucp.dev/schemas/ap2-mandates-2026-04-08.json",
        "extends": "dev.ucp.shopping.checkout"
      }
    }
  }
}
```

**Field Definitions:**

- **`name`** (string, reverse-domain format): e.g., `dev.ucp.shopping.checkout`, `com.stripe.payment_method`, `org.example.loyalty`
- **`version`** (string, YYYY-MM-DD): Date of last breaking change; used for negotiation
- **`spec`** (string, URL): Link to human-readable specification
- **`schema`** (string, URL): Link to JSON Schema definition (must have matching `name` and `version` embedded in schema)
- **`extends`** (string, optional): Parent capability name. Extensions automatically prune if parent not negotiated.

### Payment Handlers Object

Payment handlers specify how instruments are acquired and processed.

```json
{
  "ucp": {
    "payment_handlers": {
      "com.stripe.card": {
        "name": "com.stripe.card",
        "type": "tokenized",
        "required_fields": ["billing_address"],
        "spec": "https://stripe.com/ucp/handlers/card",
        "schema": "https://stripe.com/ucp/schemas/card-handler.json"
      },
      "com.google.pay": {
        "name": "com.google.pay",
        "type": "wallet",
        "required_fields": [],
        "spec": "https://developers.google.com/pay/ucp",
        "schema": "https://developers.google.com/pay/ucp/schema.json"
      }
    }
  }
}
```

**Field Definitions:**

- **`name`** (string): Handler ID (reverse-domain)
- **`type`** (string): One of `tokenized`, `wallet`, `mandate`, `bank_transfer`, `bnpl`, custom
- **`required_fields`** (array[string]): Required fields on instrument (e.g., `billing_address`, `cvv`)
- **`spec`** (string, URL): Handler specification documentation
- **`schema`** (string, URL): JSON Schema for handler-specific credential format

### Signing Keys Array

Public keys in JWK (JSON Web Key) format for RFC 9421 signature verification.

```json
{
  "signing_keys": [
    {
      "kid": "key_2026_01",
      "kty": "EC",
      "crv": "P-256",
      "x": "EXAMPLE-x-coord",
      "y": "EXAMPLE-y-coord",
      "use": "sig",
      "alg": "ES256"
    },
    {
      "kid": "key_2026_02",
      "kty": "EC",
      "crv": "P-384",
      "x": "EXAMPLE-x-coord-p384",
      "y": "EXAMPLE-y-coord-p384",
      "use": "sig",
      "alg": "ES384"
    }
  ]
}
```

**JWK Field Definitions (RFC 7517):**

- **`kid`** (string): Key identifier. Used by `Signature-Input: keyid="{kid}"` to reference this key.
- **`kty`** (string): Key type; must be `"EC"` for UCP
- **`crv`** (string): Elliptic Curve; must be `"P-256"` (required) or `"P-384"` (optional). RFC 9421 mandates P-256 minimum.
- **`x`** (string, base64url): X coordinate of public key point
- **`y`** (string, base64url): Y coordinate of public key point
- **`use`** (string): `"sig"` for signature operations
- **`alg`** (string): Algorithm; `"ES256"` (P-256), `"ES384"` (P-384), or `"ES512"` (P-521)

**Key Rotation:** Maintain multiple keys with different `kid` values. Deprecate old keys via separate process; signing responses with new key signals availability.

**Implication for our merchant:** Publish signing keys in manifest for platform verification. Implement RFC 9421 signature validation on platform requests.

---

## 2. Authentication — HTTP Message Signatures (RFC 9421)

**Specification Source:** https://ucp.dev/specification/signatures/  
**RFC Standards:** RFC 9421 (HTTP Message Signatures), RFC 9530 (Content-Digest), RFC 7517 (JWK)

HTTP Message Signatures provide cryptographic proof of request/response authenticity and integrity without requiring pre-established API keys or OAuth tokens. All HTTP-based transports (REST, MCP-over-HTTP) use RFC 9421.

### Signature Components

**Three headers** form the signature infrastructure:

1. **`Signature-Input`** (required): Describes which message components are signed
2. **`Signature`** (required): Contains the actual ECDSA signature value
3. **`Content-Digest`** (required if body present): SHA-256 hash of raw request/response body

### Request Signing Components

When a platform signs a request to the merchant, the signed components include:

```
@method
@authority
@path
@query (if query parameters present)
ucp-agent (if platform profile URL header present)
idempotency-key (for idempotent operations)
content-digest (if body exists)
content-type (if body exists)
```

**Critical:** Signatures cover raw body bytes; no JSON canonicalization is applied to the body itself. The body digest is computed via RFC 9530 (SHA-256).

### Response Signing Components

Merchant responses to platform requests sign:

```
@status (HTTP status code, e.g., "200")
content-digest
content-type
```

### Signature-Input Header Format

**Example:**

```
Signature-Input: sig=(@method @authority @path @query \
  ucp-agent idempotency-key content-digest content-type);\
  keyid="key_2026_01"; created=1715212800; alg="ecdsa-p256-sha256"
```

**Fields:**

- **`sig=(...)`**: Space-separated list of signed components. Components prefixed with `@` are special (pseudo-headers per RFC 9421).
  - `@method`: HTTP verb (GET, POST, etc.)
  - `@authority`: Target host (from Host header)
  - `@path`: Request path without query string
  - `@query`: Query string (if present)
  - `@status`: Response HTTP status (response-only)
  - Custom headers: listed by lowercase name (e.g., `ucp-agent`, `idempotency-key`)

- **`keyid="{kid}"`**: Identifies which key from `/.well-known/ucp` `signing_keys` array was used. Must match a `kid` value exactly.

- **`created=<timestamp>`**: Unix timestamp (seconds since epoch) when signature was created. Used for replay protection context.

- **`alg="ecdsa-p256-sha256"`**: Signing algorithm. Valid values:
  - `"ecdsa-p256-sha256"` — ES256 (P-256 curve, SHA-256 hash)
  - `"ecdsa-p384-sha384"` — ES384 (P-384 curve, SHA-384 hash)
  - `"ecdsa-p521-sha512"` — ES512 (P-521 curve, SHA-512 hash)

### Signature Header Format

The `Signature` header contains the actual signature value in base64url encoding.

```
Signature: sig=:EXAMPLE-signature:
```

**Format:**
- Colon-delimited base64url encoding (per RFC 8610 bytesseq syntax)
- ECDSA signature uses fixed-width **raw r||s encoding** (NOT ASN.1/DER)
  - P-256: 64 bytes total (32-byte r + 32-byte s)
  - P-384: 96 bytes total (48-byte r + 48-byte s)

### Content-Digest Header Format

```
Content-Digest: sha-256=:j3wOPfWB8Y9fU2vxWB0Z9RqXmLKNn1Ej2qVx9nYzYzk=:
```

**Computation:**
1. Take raw HTTP body bytes (no JSON normalization)
2. Compute SHA-256 hash
3. Base64url encode (RFC 4648, no padding)
4. Wrap in colon delimiters per RFC 8610

### Keyid-to-Signing-Key Mapping

**Resolution Process:**

1. Parse `Signature-Input` header
2. Extract `keyid="..."` value
3. Fetch merchant's profile from `/.well-known/ucp`
4. Locate signing key in `signing_keys` array with matching `kid` value
5. Use public key coordinates (x, y, crv) to verify signature

**If key not found:**
- Return HTTP 401 with error code `key_not_found`
- Include message: "Key ID '{kid}' not found in signer's profile"

### Merchant Verification Workflow

**Step-by-step verification for platform requests:**

```
verify_request(request):
  1. Parse request Signature-Input header
  2. Extract keyid, created timestamp, alg
  3. Fetch platform's profile URL from UCP-Agent header
     → Validate it's a valid HTTPS URL
  4. GET /.well-known/ucp on platform's domain
     → Cache with 24-hour TTL (profile changes are rare)
  5. Locate signing_keys entry with matching kid
     → Abort with 401 key_not_found if not found
  6. Extract public key coordinates (x, y) and curve (crv)
  7. Reconstruct signature base per RFC 9421:
     a. For each component in Signature-Input:
        - Fetch value from request (@method, @path, headers, etc.)
        - Format as: '"componentname"' + ": " + value
        - Append newline
     b. Remove trailing newline
  8. Compute body digest:
     a. SHA-256(raw request body bytes)
     b. Base64url encode
  9. Verify Content-Digest header matches computed digest
     → Abort with 400 digest_mismatch if no match
  10. Verify ECDSA signature:
      - Decode signature from base64url (must be 64 bytes for P-256, 96 for P-384)
      - Split into r (first half) and s (second half)
      - ECDSA-verify(message=signature_base, r, s, public_key)
     → Abort with 401 signature_invalid if verification fails
  11. Accept request, record signer identity as platform with UCP-Agent URL
```

### MCP Transport Equivalent

For MCP (Model Context Protocol over JSON-RPC), the signature mechanism remains identical:
- HTTP body = JSON-RPC message body
- `Content-Digest` = SHA-256(JSON-RPC bytes)
- Signature components = same as HTTP

The `meta.ucp-agent` field in MCP requests (described in section 3) serves the same discovery role as HTTP `UCP-Agent` header.

### Profile Caching Guidance

- **Cache TTL:** 24–48 hours (profiles change infrequently)
- **Invalidation:** Serve new Content-Digest if profile URL content changes
- **Fallback:** If fetch fails, allow cached profile to remain valid
- **Security:** Always validate `kid` exists in cached profile before accepting signature

### Error Codes

Signature-related failures return:

| Code | HTTP Status | Meaning |
|------|-------------|---------|
| `signature_missing` | 401 | Required Signature-Input or Signature header absent |
| `signature_invalid` | 401 | ECDSA verification failed |
| `key_not_found` | 401 | `keyid` does not exist in signer's signing_keys |
| `digest_mismatch` | 400 | Content-Digest value does not match computed body hash |
| `profile_unreachable` | 424 | Could not fetch signer's `/.well-known/ucp` profile |
| `invalid_profile_url` | 401 | UCP-Agent header contains invalid URL |

**Implication for our merchant:** Implement RFC 9421 signature verification on incoming platform requests. Cache platform profiles. Return appropriate error codes on verification failure. Verify body digest to detect tampering.

---

## 3. Agent Profile

**Specification Source:** https://ucp.dev/latest/specification/checkout/  

An agent profile is the JSON document a merchant (or platform) hosts at its profile URL, which is advertised via the `UCP-Agent` header and `meta.ucp-agent.profile` in MCP contexts.

### Profile URL Discovery

**HTTP/REST:**
```
Request Header: UCP-Agent: profile="https://merchant.example/.well-known/ucp"
```

**MCP:**
```json
{
  "meta": {
    "ucp-agent": {
      "profile": "https://merchant.example/.well-known/ucp"
    }
  }
}
```

The profile URL must be:
- Absolute HTTPS URL
- Publicly accessible (no authentication required for discovery)
- Hosted at standard location `/.well-known/ucp` (RECOMMENDED) or custom path (MAY)

### Agent Profile JSON Structure

**Complete example:**

```json
{
  "ucp": {
    "version": "2026-04-08",
    "services": {
      "dev.ucp.shopping": {
        "rest": {
          "endpoint": "https://merchant.example/ucp/checkout",
          "schema": "https://merchant.example/schemas/checkout.json"
        }
      }
    },
    "capabilities": {
      "dev.ucp.shopping.checkout": {
        "version": "2026-04-08",
        "spec": "https://ucp.dev/specification/checkout",
        "schema": "https://ucp.dev/schemas/checkout.json"
      },
      "dev.ucp.shopping.ap2_mandate": {
        "version": "2026-04-08",
        "spec": "https://ucp.dev/specification/ap2-mandates",
        "schema": "https://ucp.dev/schemas/ap2-mandates.json",
        "extends": "dev.ucp.shopping.checkout"
      },
      "dev.ucp.common.identity_linking": {
        "version": "2026-04-08",
        "spec": "https://ucp.dev/specification/identity-linking",
        "schema": "https://ucp.dev/schemas/identity-linking.json"
      }
    },
    "payment_handlers": {
      "com.stripe.card": {
        "name": "com.stripe.card",
        "type": "tokenized",
        "spec": "https://stripe.com/ucp/card",
        "schema": "https://stripe.com/ucp/schemas/card.json"
      }
    }
  },
  "signing_keys": [
    {
      "kid": "key_2026_01",
      "kty": "EC",
      "crv": "P-256",
      "x": "EXAMPLE-x-coord",
      "y": "EXAMPLE-y-coord",
      "use": "sig",
      "alg": "ES256"
    }
  ]
}
```

**Fields are identical to the Manifest (section 1).** The profile is the manifest document itself.

### Header Binding Verification

**Critical requirement:** Verifiers MUST ensure the authenticated identity is consistent with the `UCP-Agent` header.

```
Pseudocode:
verify_signature_consistency(request):
  ucp_agent_url = request.headers["UCP-Agent"]
    → parse profile="https://..." value
  
  1. Fetch profile from ucp_agent_url
  2. Extract public key for signature verification
  3. Verify request signature using this key
  4. Bind request to authenticated identity = ucp_agent_url
  
  // Prevents: attacker using key A to sign request, then claiming to be identity B
```

**Implication for our merchant:** Validate that the profile URL in UCP-Agent header matches the origin of the fetched profile. Bind authenticated requests to the profile URL, not just the key ID.

---

## 4. Capability Negotiation & Intersection

**Specification Source:** https://ucp.dev/documentation/core-concepts/ | https://ucp.dev/specification/overview

Capability negotiation determines which features both parties support at compatible versions, enabling dynamic interoperability without pre-configuration.

### Negotiation Model: Server-Selects Architecture

**Principle:** The merchant (server) determines the active capabilities from the intersection of both parties' declared capabilities.

1. **Platform advertises** its capabilities in its profile (via `UCP-Agent` header)
2. **Merchant receives** platform profile URL
3. **Merchant fetches** platform's `/.well-known/ucp`
4. **Merchant computes** intersection
5. **Merchant includes** active capabilities in every response's `ucp.capabilities` field
6. **Platform observes** which capabilities are active

### Intersection Algorithm

**Three-phase process:**

**Phase 1: Match by Name**
- Identify capabilities appearing in both merchant and platform declarations
- Capability name is reverse-domain (e.g., `dev.ucp.shopping.checkout`)

Example:
```
Merchant capabilities:    Platform capabilities:
- dev.ucp.shopping.checkout         - dev.ucp.shopping.checkout
- dev.ucp.shopping.cart             - dev.ucp.shopping.cart
- dev.ucp.shopping.order            - dev.ucp.common.identity_linking
- com.stripe.payment_method         - dev.ucp.shopping.ap2_mandate
```
→ Matched names: `dev.ucp.shopping.checkout`, `dev.ucp.shopping.cart`

**Phase 2: Version Selection**
- For each matched capability name, select the highest mutually compatible version
- Versions are date-based (YYYY-MM-DD); higher dates = newer versions
- Compatibility: both parties declare the same version

Example:
```
Capability: dev.ucp.shopping.checkout
  Merchant version: 2026-04-08
  Platform version: 2026-04-08
  → Selected: 2026-04-08 ✓

Capability: dev.ucp.shopping.cart
  Merchant version: 2026-01-15
  Platform version: 2026-04-08
  → No common version; PRUNE this capability ✗
```

**Phase 3: Extension Pruning**
- Remove extensions whose parent capability is not in the intersection
- Repeat until no orphaned extensions remain

Example:
```
Merchant declares:
  - dev.ucp.shopping.ap2_mandate (extends: dev.ucp.shopping.checkout)
  - dev.ucp.shopping.fulfillment (extends: dev.ucp.shopping.checkout)

Platform declares:
  - dev.ucp.shopping.ap2_mandate

Phase 1: Both declare ap2_mandate, checkout
Phase 2: Select versions (assume all match)
Phase 3: Both ap2_mandate and fulfillment have checkout as parent
         → fulfillment has no platform support; PRUNE
         → ap2_mandate has platform support; KEEP

Result: Active capabilities = [checkout, ap2_mandate]
```

### Response Representation: `ucp.capabilities` Object

Every response includes the negotiated capability set:

```json
{
  "ucp": {
    "version": "2026-04-08",
    "status": "success",
    "capabilities": {
      "dev.ucp.shopping.checkout": {
        "version": "2026-04-08",
        "spec": "https://ucp.dev/specification/checkout",
        "schema": "https://ucp.dev/schemas/checkout.json"
      },
      "dev.ucp.shopping.ap2_mandate": {
        "version": "2026-04-08",
        "spec": "https://ucp.dev/specification/ap2-mandates",
        "schema": "https://ucp.dev/schemas/ap2-mandates.json",
        "extends": "dev.ucp.shopping.checkout"
      }
    },
    "payment_handlers": {
      "com.stripe.card": {
        "name": "com.stripe.card",
        "type": "tokenized",
        "spec": "https://stripe.com/ucp/card"
      }
    }
  },
  "id": "checkout_abc123",
  "status": "incomplete",
  ...
}
```

**Key insight:** Platform observes `ucp.capabilities` in response to determine which features the merchant actually supports. Simplifies platform logic: no pre-configuration, discovered at runtime.

### Namespace Authority Validation

**Reverse-domain governance:** Capability names encode authority.

- `dev.ucp.*` — UCP governing body (central authority)
- `com.stripe.*` — Stripe controls this namespace
- `org.example.*` — example.org controls this namespace

**Validation requirement:** The `spec` and `schema` URLs must originate from the namespace authority domain.

**Example validation:**

```
Capability: dev.ucp.shopping.checkout
Declared spec: https://ucp.dev/specification/checkout
Namespace authority: dev.ucp (reverse of "ucp.dev")
Actual authority: ucp.dev
✓ Valid: spec URL originates from ucp.dev

Capability: com.stripe.payment_method
Declared spec: https://example-attacker.com/stripe-spec
Namespace authority: com.stripe (reverse of "stripe.com")
Actual authority: example-attacker.com
✗ Invalid: spec URL does not originate from stripe.com
```

**Implication for our merchant:** Validate namespace authority on received capabilities. Publish merchant capabilities in manifest. Include negotiated capabilities in every response. Prune unsupported extensions.

---

## 5. Checkout Capability

**Specification Source:** https://ucp.dev/specification/checkout/  
**REST Binding:** https://ucp.dev/specification/checkout-rest/  
**MCP Binding:** https://ucp.dev/specification/checkout-mcp/

The checkout capability enables merchants to manage purchase sessions from creation through order completion.

### Checkout Status Lifecycle

Six mutually exclusive status values govern checkout progression:

| Status | Meaning | Next States | Platform Action |
|--------|---------|-------------|-----------------|
| `incomplete` | Missing required info; has errors with `severity: recoverable` | `requires_escalation`, `ready_for_complete`, `canceled` | Call Update Checkout to fix errors; retry |
| `requires_escalation` | Needs info unavailable via API; has `requires_buyer_input` or `requires_buyer_review` errors | `incomplete`, `canceled` | Hand off to merchant UI via `continue_url` |
| `ready_for_complete` | All info present; can finalize programmatically | `complete_in_progress`, `canceled` | Call Complete Checkout |
| `complete_in_progress` | Merchant processing completion | `completed`, `requires_escalation` | Wait for final status; poll Get Checkout |
| `completed` | Order successfully placed; immutable | (none) | Transaction finalized |
| `canceled` | Session terminated or expired | (none) | Initiate new checkout if needed |

### Create Checkout Request

**HTTP REST:**
```
POST /checkout-sessions
Headers:
  UCP-Agent: profile="https://platform.example/.well-known/ucp"
  Idempotency-Key: 550e8400-e29b-41d4-a716-446655440000
  Content-Type: application/json
  [Signature-Input, Signature, Content-Digest (if signed)]
```

**MCP:**
```json
{
  "method": "tools/call",
  "arguments": {
    "name": "create_checkout",
    "arguments": {
      "meta": {
        "ucp-agent": {
          "profile": "https://platform.example/.well-known/ucp"
        },
        "idempotency-key": "550e8400-e29b-41d4-a716-446655440000"
      },
      "checkout": { /* checkout object */ }
    }
  }
}
```

**Request Body / MCP `checkout` Parameter:**

```json
{
  "line_items": [
    {
      "item": {
        "id": "SKU_123",
        "title": "Widget Pro",
        "price": {
          "amount": 2999,
          "currency": "USD"
        },
        "image_url": "https://merchant.example/images/widget.jpg",
        "url": "https://merchant.example/products/widget-pro"
      },
      "quantity": 2
    }
  ],
  "buyer": {
    "first_name": "Jane",
    "last_name": "Doe",
    "email": "jane@example.com",
    "phone_number": "+15551234567",
    "postal_address": {
      "country": "US",
      "region": "CA",
      "city": "San Francisco",
      "postal_code": "94102",
      "address_line_1": "123 Main St",
      "address_line_2": "Suite 100"
    }
  },
  "context": {
    "address_country": "US",
    "address_region": "CA",
    "postal_code": "94102",
    "language": "en-US",
    "currency": "USD",
    "eligibility": [
      "com.example.loyalty_gold",
      "dev.ucp.buyer.verified"
    ]
  },
  "signals": {
    "dev.ucp.buyer_ip": "192.0.2.1",
    "dev.ucp.user_agent": "Mozilla/5.0..."
  },
  "payment": {
    "instruments": []
  },
  "attribution": {
    "referrer": "https://search.example.com/results?q=widget"
  }
}
```

**Field Definitions:**

**`line_items` (array, required):**
- **`item`** (object, required):
  - **`id`** (string): SKU or product identifier
  - **`title`** (string): Display name
  - **`price`** (object):
    - **`amount`** (integer): Monetary amount in currency's minor unit (e.g., cents for USD)
    - **`currency`** (string): ISO 4217 code (e.g., "USD")
  - **`image_url`** (string, optional): Product image URL
  - **`url`** (string, optional): Product details URL
- **`quantity`** (integer): Quantity of this item

**`buyer` (object, optional):**
- **`first_name`** (string): Given name
- **`last_name`** (string): Family name
- **`email`** (string): Email address
- **`phone_number`** (string): E.164 format (e.g., "+15551234567")
- **`postal_address`** (object, optional):
  - **`country`** (string): ISO 3166-1 alpha-2 (e.g., "US")
  - **`region`** (string): State/province code
  - **`city`** (string): City name
  - **`postal_code`** (string): ZIP or postal code
  - **`address_line_1`** (string): Street address
  - **`address_line_2`** (string, optional): Apartment, suite, etc.

**`context` (object, optional):** Provisional market signals
- **`address_country`** (string): ISO 3166-1 alpha-2
- **`address_region`** (string): State/province code
- **`postal_code`** (string): ZIP
- **`language`** (string): IETF BCP 47 (e.g., "en-US")
- **`currency`** (string): ISO 4217
- **`eligibility`** (array[string]): Buyer claims (loyalty, offers, etc.) in reverse-domain format
  - Example: `["com.example.loyalty_gold", "dev.ucp.buyer.verified"]`
  - **CRITICAL:** Platform provides claims; merchant must verify before completion

**`signals` (object, optional):** Environment data
- **`dev.ucp.buyer_ip`** (string): IPv4 or IPv6 address
- **`dev.ucp.user_agent`** (string): HTTP User-Agent header
- **Other signals:** Custom platform-provided signals (fraud scores, device fingerprints, etc.)

**`payment` (object, optional):**
- **`instruments`** (array): Collected payment credentials. Initially empty on Create; populated by platform before Complete.

**`attribution` (object, optional):**
- **`referrer`** (string, URL): Source of purchase intent (for analytics)

### Checkout Response

**HTTP REST:**
```
HTTP/1.1 200 OK
Headers:
  Content-Type: application/json
  Content-Digest: sha-256=:...
  [Signature-Input, Signature (if response signing)]
```

**MCP:**
```json
{
  "method": "tools/call",
  "result": { /* entire checkout object */ }
}
```

**Response Body:**

```json
{
  "ucp": {
    "version": "2026-04-08",
    "status": "success",
    "capabilities": {
      "dev.ucp.shopping.checkout": {
        "version": "2026-04-08"
      }
    },
    "payment_handlers": {
      "com.stripe.card": {
        "name": "com.stripe.card",
        "type": "tokenized"
      }
    }
  },
  "id": "checkout_abc123def456",
  "status": "incomplete",
  "currency": "USD",
  "line_items": [
    {
      "id": "li_1",
      "item": {
        "id": "SKU_123",
        "title": "Widget Pro",
        "price": {
          "amount": 2999,
          "currency": "USD"
        }
      },
      "quantity": 2,
      "totals": [
        {
          "type": "subtotal",
          "display_text": "Subtotal",
          "amount": 5998
        }
      ]
    }
  ],
  "buyer": { /* echo of request */ },
  "context": { /* echo of request */ },
  "totals": [
    {
      "type": "subtotal",
      "display_text": "Subtotal",
      "amount": 5998
    },
    {
      "type": "tax",
      "display_text": "Sales Tax",
      "amount": 475
    },
    {
      "type": "fulfillment",
      "display_text": "Shipping",
      "amount": 799
    },
    {
      "type": "total",
      "display_text": "Grand Total",
      "amount": 7272
    }
  ],
  "messages": [
    {
      "type": "error",
      "code": "address_required",
      "severity": "recoverable",
      "content": "Delivery address is required"
    }
  ],
  "payment": {
    "instruments": []
  },
  "links": [
    {
      "type": "privacy_policy",
      "url": "https://merchant.example/privacy",
      "title": "Privacy Policy"
    }
  ],
  "continues_url": null,
  "expires_at": "2026-01-20T18:30:00Z"
}
```

**Field Definitions:**

**`ucp`** (object, required):
- **`version`** (string): Protocol version (e.g., "2026-04-08")
- **`status`** (string): "success" or "error"
- **`capabilities`** (object): Negotiated capabilities (see section 4)
- **`payment_handlers`** (object): Available payment handlers

**`id`** (string): Unique checkout session identifier. Must be stable across Get/Update/Complete operations.

**`status`** (string): One of the six statuses listed in the table above.

**`currency`** (string): ISO 4217 code

**`line_items`** (array):
- **`id`** (string): Line item identifier
- **`item`** (object): Product details (echoed from request)
- **`quantity`** (integer): Quantity
- **`totals`** (array):
  - **`type`** (string): "subtotal", "tax", "discount", "fulfillment", "fee", or custom
  - **`display_text`** (string): Human-readable label
  - **`amount`** (integer): Minor unit amount
  - **`sub_lines`** (array, optional): Itemized breakdown (e.g., 5% tax + 2% local tax)

**`buyer`**, **`context`** (objects): Echo of request data

**`totals`** (array, required): **CRITICAL:** Platform MUST render all entries in order provided; CANNOT reorder, filter, or aggregate.
- **`type`** (string): "subtotal", "discount", "fulfillment", "tax", "fee", "total" (and custom)
- **`display_text`** (string): Display label
- **`amount`** (integer): Amount in minor units (can be negative for discounts)
  - Sign convention: positive for charges, negative for credits
- **`sub_lines`** (array, optional): Detail breakdown (must sum to parent `amount`)

**`messages`** (array, optional): Errors, warnings, informational content

**`payment.instruments`** (array): Initially empty on Create. Platform populates before Complete with collected payment data.

**`links`** (array, optional): Associated URLs
- **`type`** (string): "privacy_policy", "terms_of_service", "returns_policy", custom
- **`url`** (string): HTTPS URL
- **`title`** (string): Display text

**`continues_url`** (string, optional, **REQUIRED if status is `requires_escalation`**): Absolute HTTPS URL where platform redirects buyer for manual intervention.

**`expires_at`** (string, ISO 8601 datetime): Session expiration timestamp

### Error Handling

**Message Object Structure:**

```json
{
  "type": "error|warning|info",
  "code": "string",
  "severity": "recoverable|requires_buyer_input|requires_buyer_review|unrecoverable",
  "content": "string",
  "content_type": "plain|markdown",
  "path": "$.line_items[0].item.price",
  "image_url": "https://example.com/icon.svg",
  "url": "https://example.com/help",
  "presentation": "notice|disclosure"
}
```

**Severity Mapping to Platform Action:**

| Severity | Scenario | Action |
|----------|----------|--------|
| `recoverable` | Platform can fix via Update Checkout | Inspect error code; call Update Checkout with corrected data; retry |
| `requires_buyer_input` | Business needs info platform can't provide (e.g., delivery window choice) | Hand off to merchant UI via `continue_url` with context |
| `requires_buyer_review` | Regulatory/policy authorization needed | Hand off to merchant UI via `continue_url` |
| `unrecoverable` | No valid resource exists | Retry with new items/inputs; if still fails, escalate via `continue_url` |

**Standard Error Codes (return with `severity: recoverable` to signal API-fixable):**

- `out_of_stock` — Item/variant unavailable
- `item_unavailable` — Item cannot be purchased (legacy product, restricted region)
- `address_undeliverable` — Delivery impossible to provided address
- `address_required` — Delivery address missing
- `payment_failed` — Payment processing failed
- `eligibility_invalid` — Eligibility claim verification failed at completion
- `invalid_phone` — Phone number format invalid

**Error Processing Algorithm:**

```
errors = response.messages.filter(m => m.type == "error")

IF any error.severity == "unrecoverable":
  RECOMMEND retry with new checkout or escalate via continue_url
ELSE IF any error.severity == "recoverable":
  TRY:
    Fix error (e.g., update address)
    Call Update Checkout
    Retry
ELSE IF any error.severity == "requires_buyer_input" OR "requires_buyer_review":
  REQUIRE continue_url present
  Redirect buyer to continue_url with error context
```

### Payment Instruments

**Structure in `payment.instruments` array:**

```json
{
  "payment": {
    "instruments": [
      {
        "id": "instr_stripe_card_001",
        "handler_id": "com.stripe.card",
        "type": "tokenized_card",
        "billing_address": {
          "country": "US",
          "region": "CA",
          "city": "San Francisco",
          "postal_code": "94102",
          "address_line_1": "123 Main St"
        },
        "credential": {
          "type": "stripe_payment_method_id",
          "token": "pm_EXAMPLE"
        },
        "display": {
          "card_brand": "Visa",
          "card_last_four": "4242",
          "card_expiry": "12/25"
        },
        "selected": true
      }
    ]
  }
}
```

**Field Definitions:**

- **`id`** (string): Unique instrument identifier within this checkout
- **`handler_id`** (string): References a handler in `ucp.payment_handlers` (e.g., "com.stripe.card")
- **`type`** (string): Handler-specific type (e.g., "tokenized_card", "wallet", "bank_account")
- **`billing_address`** (object): Postal address for this instrument
- **`credential`** (object): Opaque credential data per handler specification
  - Structure varies by handler (defined in handler schema, not here)
  - Platform provides; merchant must NOT interpret or store raw values
- **`display`** (object): Merchant-friendly display properties per handler
  - Handler-defined (e.g., card brand, last 4 digits, expiry)
  - Safe for rendering
- **`selected`** (boolean): Whether this instrument is the active payment method

### Eligibility & Claims Verification

**Platform provides buyer claims in `context.eligibility`:**

```json
{
  "context": {
    "eligibility": [
      "com.example.loyalty_gold",
      "dev.ucp.buyer.verified",
      "com.apple.pay"
    ]
  }
}
```

**Merchant must verify all claims before completion:**

```
verify_eligibility(claims):
  FOR each claim in claims:
    IF merchant does not recognize claim:
      SKIP (merchant MAY NOT block checkout)
    ELSE:
      Verify claim against authoritative source
      IF verification fails:
        Return error {
          type: "error",
          code: "eligibility_invalid",
          severity: "recoverable",
          path: "$.context.eligibility[<index>]",
          content: "Loyalty status not confirmed"
        }
```

**CRITICAL:** Merchants MUST NOT complete checkout with unresolved eligibility claims.

### Continue URL

**When Required:**
- MANDATORY when `status: requires_escalation`
- RECOMMENDED for non-terminal statuses

**Format:**
```
https://merchant.example/checkout-sessions/abc123?error_code=address_required
```

**Approaches:**

1. **Server-side state (RECOMMENDED):** `https://merchant.example/checkout-sessions/{checkout_id}`
   - Merchant stores session state server-side
   - Platform redirects with opaque session ID
   - Merchant retrieves state and renders UI

2. **Stateless checkout permalink:** `https://merchant.example/checkout?state=<encoded_checkout>`
   - Encode entire checkout response in URL or session
   - Merchant parses and renders UI
   - Buyer makes changes; saves back to platform

### Core Operations

#### 1. Create Checkout

**Method:** POST /checkout-sessions (REST) | create_checkout (MCP)

**Idempotency:** Required. `Idempotency-Key` header must be present.
- Same key + same request body = cached response
- Server caches for 24–48 hours
- Retry-safe: duplicate request returns same checkout ID

**Response:** Checkout object with initial status (usually `incomplete` or `ready_for_complete`)

#### 2. Get Checkout

**Method:** GET /checkout-sessions/{id} (REST) | get_checkout(id) (MCP)

**Purpose:** Retrieve current checkout state

**Response:** Complete checkout object with current status

#### 3. Update Checkout

**Method:** PUT /checkout-sessions/{id} (REST) | update_checkout(id, checkout) (MCP)

**Semantics:** Full resource replacement (not PATCH)

**Request body:** Complete checkout object with modifications

**Response:** Updated checkout object

#### 4. Complete Checkout

**Method:** POST /checkout-sessions/{id}/complete (REST) | complete_checkout(id) (MCP)

**Idempotency:** Required. `Idempotency-Key` header must be present.

**Request body (REST):** Typically empty or minimal (payment instruments may be included)

**Request body (MCP):**
```json
{
  "meta": {
    "ucp-agent": { "profile": "..." },
    "idempotency-key": "550e8400..."
  },
  "id": "checkout_abc123"
}
```

**Response:** Checkout object with `status: complete_in_progress` or `completed`. Includes new `order` field:

```json
{
  "id": "checkout_abc123",
  "status": "completed",
  "order": {
    "id": "order_xyz789",
    "created_at": "2026-01-15T10:30:00Z",
    "order_number": "ORD-2026-001234"
  },
  ...
}
```

**Merchant must:**
- Verify all eligibility claims
- Validate payment instruments
- Allocate inventory
- Create order record
- Send confirmation email to buyer
- Return `completed` status (or `complete_in_progress` if async processing)

#### 5. Cancel Checkout

**Method:** POST /checkout-sessions/{id}/cancel (REST) | cancel_checkout(id) (MCP)

**Semantics:** Terminate session; only non-terminal sessions can be canceled.

**Response:** Cancelled checkout object with `status: canceled`

### Totals Rendering Contract

**Platform MUST render all totals in order provided. Absolutely NO reordering, filtering, aggregation, or application of display logic.**

**Merchant determines:**
- Which total entries to include
- Order of entries
- Display text
- Numeric values

**Platform responsibility:**
- Render in exact order
- Preserve sign convention (positive = charge, negative = credit)
- Display with appropriate currency formatting

**Well-known total types (for reference):**
- `subtotal` — Base price sum (always positive)
- `discount` — Order-level discount (always negative)
- `fulfillment` — Shipping/delivery (can be positive or zero)
- `tax` — Sales/VAT tax (usually positive)
- `fee` — Payment or service fee (positive)
- `total` — Grand total (must appear exactly once, should equal sum of all others)

**Sub-lines example:**

```json
{
  "type": "tax",
  "display_text": "Tax",
  "amount": 480,
  "sub_lines": [
    {
      "type": "tax",
      "display_text": "Sales Tax (5%)",
      "amount": 300
    },
    {
      "type": "tax",
      "display_text": "Local Tax (3%)",
      "amount": 180
    }
  ]
}
```

### Warning Messages & Disclosure Rendering

**Warnings with `presentation: "disclosure"` demand specific treatment:**

```json
{
  "type": "warning",
  "code": "allergens",
  "severity": null,
  "content": "**Contains: tree nuts.** Produced in a facility...",
  "content_type": "markdown",
  "presentation": "disclosure",
  "path": "$.line_items[0]",
  "image_url": "https://example.com/allergen-icon.svg",
  "url": "https://example.com/allergen-info"
}
```

**Platform requirements:**
- MUST display warning content to buyer in proximity to referenced component
- MUST NOT hide, collapse, or auto-dismiss
- MUST render `image_url` and `url` when present
- If unable to honor contract: escalate to merchant UI via `continue_url`

**Merchant requirements:**
- Use `presentation: "disclosure"` for safety warnings, allergen declarations, compliance notices
- Populate `path` (JSONPath RFC 9535) associating disclosure with specific component
- Provide `code` identifying disclosure category (e.g., `prop65`, `allergens`)

**Implication for our merchant:** Implement all five operations with proper status transitions. Return error messages with correct severity. Render totals in exact order. Validate eligibility claims before completion. Include AP2 mandate in response when negotiated.

---

## 6. AP2 Mandate (`dev.ucp.shopping.ap2_mandate`)

**Specification Source:** https://ucp.dev/specification/ap2-mandates/

The AP2 Mandates extension enables cryptographic proof of transaction authorization through Verifiable Digital Credentials, allowing autonomous agent-led checkout without human intervention.

### Mandate Structure

Two distinct mandate artifacts are required:

**1. Checkout Mandate** (`ap2.checkout_mandate`):
- Type: W3C-VC 2.0 envelope with pragmatic raw ECDSA P-256 r||s proof (not SD-JWT; full SD-JWT+kb and JsonWebSignature2020 are deferred per DESIGN §13).
- Purpose: Protects merchant interests by binding platform's signature to business's authorization
- Content: Full checkout response with embedded merchant signature
- Signed by: Platform (creates outer signature)

**2. Payment Mandate** (`payment.instruments[*].credential.token`):
- Type: W3C-VC 2.0 envelope with pragmatic raw ECDSA P-256 r||s proof (not SD-JWT; full SD-JWT+kb and JsonWebSignature2020 are deferred per DESIGN §13).
- Purpose: Protects fund authorization
- Signed by: Payment Service Provider (PSP)

> **Travel Guild submission scope:** the checkout mandate uses the `checkoutMandateVC` W3C-VC shape; the payment mandate is SIMULATED (see PSP section below).

### Business Authorization (Merchant Signature)

**Format:** JWS Detached Content per RFC 7515 Appendix F

Merchant embeds cryptographic proof of authorization in the response:

```json
{
  "ap2": {
    "merchant_authorization": "<jws_header>..<signature>",
    "checkout_mandate": "eyJhbGci...",
    "signing_key_id": "key_2026_01"
  }
}
```

**JWS Detached Content format:**
- `<jws_header>` — Base64url-encoded JSON: `{"alg":"ES256","kid":"key_2026_01"}`
- `..` — Double dot indicating detached payload
- `<signature>` — Base64url-encoded ECDSA signature (64 bytes for ES256)

Example: `<jws-header>..EXAMPLE-signature`

> **Travel Guild implementation note:** In the Travel Guild submission, `merchant_authorization` is a raw base64-encoded ECDSA-P256 signature over the pipe-delimited mandate base string (`UserID|CheckoutID|BudgetCents|Currency|ValidUntil`), not a JWS Detached Content structure. RFC 8785 JCS canonicalization and the full JWS path are deferred per DESIGN §13. The `**Format: JWS Detached Content**` description above is the UCP spec target, not the current implementation.

### Signature Computation

**Merchant signing process:**

```
1. Take complete checkout response
2. EXCLUDE the entire "ap2" field
3. Canonicalize using JSON Canonicalization Scheme (RFC 8785):
   - Serialize object properties in alphabetically sorted order
   - Use no whitespace
   - Produce byte-identical output for semantically identical JSON
4. Create JWS header: {"alg":"ES256","kid":"<key_id>"}
5. Sign using ECDSA with P-256 private key:
   - Message to sign = JWS_header || '.' || Canonicalized_checkout
6. Encode signature as base64url
7. Return: header .. signature (detached format)
```

**Why JCS (RFC 8785)?**
Mandates are stored evidence transmitted across systems over time. JCS ensures "semantically identical JSON produces byte-identical output, making signatures reproducible across implementations" even when JSON is re-serialized.

### Verification Flow

#### Platform Verification of Business Authorization

When platform receives completion request with mandate:

```
1. Extract ap2.merchant_authorization (JWS detached format)
2. Parse header: decode first part (before first dot)
3. Extract kid from header
4. Fetch merchant's /.well-known/ucp profile
5. Locate signing_keys entry with matching kid
6. Extract public key coordinates (x, y) and curve (crv)
7. Reconstruct canonical checkout:
   a. Take checkout_mandate (SD-JWT)
   b. Decode and verify SD-JWT signature per AP2 spec
   c. Extract claims (contains checkout payload)
   d. Remove ap2 field
   e. Canonicalize using RFC 8785
8. Verify ECDSA signature:
   a. Decode signature from base64url
   b. Split into r (first 32 bytes) and s (second 32 bytes)
   c. ECDSA-verify(message=canonical_checkout, r, s, public_key)
9. If verification succeeds: merchant explicitly authorized this checkout
```

#### Business Verification Upon Completion

When merchant receives complete request:

```
1. Verify mandate's validity per AP2 Protocol specifications
   - Check SD-JWT signature and key binding
   - Verify not expired (check valid_until timestamp)
   - Verify budget not exceeded
2. Extract embedded checkout from verified mandate claims
3. Re-verify merchant's own merchant_authorization signature:
   a. Decode detached JWS
   b. Reconstruct canonical checkout
   c. Verify signature matches current session
4. Confirm terms match current session:
   - Total amount, currency, items, buyer
   - If mismatch: REJECT with error code mandate_mismatch
5. Proceed with order creation
```

#### Payment Service Provider Verification

> **SUBMISSION SCOPE NOTE:** In the Travel Guild submission, PSP-side fund authorization is SIMULATED via `alipay_sim.go` (sandbox, no real payment rail). The `paymentMandate` struct carries `Simulated:true`. A real SD-JWT PSP credential and live Alipay integration is KIV (requires a business account). The W3C-VC two-tier mandate protocol shape is real; the settlement leg is not.

PSPs verify the `payment_mandate` in `payment.instruments[*].credential.token` per AP2 specification:
- Signature validity
- Expiration (valid_until)
- Checkout hash correlation
- Budget constraints

### Mandate Structure in Response

**Complete example with AP2:**

```json
{
  "ucp": {
    "version": "2026-04-08",
    "status": "success",
    "capabilities": {
      "dev.ucp.shopping.checkout": { "version": "2026-04-08" },
      "dev.ucp.shopping.ap2_mandate": { "version": "2026-04-08" }
    }
  },
  "id": "checkout_abc123",
  "status": "ready_for_complete",
  "currency": "USD",
  "line_items": [ /* ... */ ],
  "totals": [ /* ... */ ],
  "buyer": { /* ... */ },
  "ap2": {
    "merchant_authorization": "<jws-header>..EXAMPLE-signature",
    "checkout_mandate": "<sd-jwt-checkout-mandate>",
    "signing_key_id": "key_2026_01"
  },
  "payment": {
    "instruments": [
      {
        "id": "instr_1",
        "handler_id": "com.stripe.card",
        "credential": {
          "token": "<sd-jwt-payment-mandate>"
        }
      }
    ]
  }
}
```

**Field Definitions:**

- **`ap2.merchant_authorization`** (string, required): JWS Detached Content authorizing this checkout
- **`ap2.checkout_mandate`** (string, required): SD-JWT credential created by platform, containing the checkout
- **`ap2.signing_key_id`** (string): Key ID (`kid`) used to sign merchant_authorization. For reference; embedded in JWS header.

### Error Handling

When AP2 mandate processing fails, return standard checkout errors:

| Error Code | Scenario | Severity |
|------------|----------|----------|
| `mandate_required` | AP2 negotiated but request lacks mandate | `unrecoverable` |
| `mandate_invalid_signature` | Signature verification failed | `unrecoverable` |
| `mandate_expired` | Mandate timestamp exceeded valid_until | `unrecoverable` |
| `mandate_mismatch` | Mandate checkout terms differ from session | `unrecoverable` |
| `merchant_authorization_invalid` | Business signature unverifiable | `unrecoverable` |
| `budget_exceeded` | Mandate budget constraint violated | `recoverable` (may retry with lower amount) |

### Discovery & Activation

**Merchant advertises support:**

```json
{
  "ucp": {
    "capabilities": {
      "dev.ucp.shopping.ap2_mandate": {
        "version": "2026-04-08",
        "spec": "https://ucp.dev/specification/ap2-mandates",
        "schema": "https://ucp.dev/schemas/ap2-mandates.json",
        "extends": "dev.ucp.shopping.checkout"
      }
    }
  },
  "signing_keys": [ /* ... */ ]
}
```

**Activation:** Through standard UCP capability negotiation (section 4).
- If both platform and merchant declare `dev.ucp.shopping.ap2_mandate` at same version
- AND both have `dev.ucp.shopping.checkout` negotiated
- → AP2 is active; merchant must include `ap2` in responses

**Implication for our merchant:** Implement JWS detached signing of checkout using RFC 8785 canonicalization. Verify mandates from platform before completion. Return merchant_authorization in ap2 field when AP2 is negotiated. Validate budget constraints.

---

## 7. Identity Linking (OAuth 2.0)

**Specification Source:** https://ucp.dev/specification/identity-linking/

Identity linking enables platforms to obtain user authorization for personalized commerce operations (order history, loyalty benefits, saved addresses).

### Three-Party Model

```
┌──────────┐        OAuth 2.0          ┌──────────┐
│ Platform │ ◄──────────────────────► │ Business │
│          │    Authorization Code     │ (OAuth   │
│          │                           │  Server) │
└──────────┘                           └──────────┘
     ▲
     │ User grants consent
     │
┌────┴──────┐
│    User   │
└───────────┘
```

**Key Participants:**
- **Platform**: Initiates identity linking (user agent)
- **Business**: Authorization server (hosts OAuth endpoints)
- **User**: Resource owner; grants explicit consent

### OAuth 2.0 Implementation

**Flow:** Authorization Code with PKCE (Proof Key for Code Exchange)

**Merchant requirements:**

1. Publish authorization server metadata at `/.well-known/oauth-authorization-server` per RFC 8414

```json
{
  "issuer": "https://merchant.example",
  "authorization_endpoint": "https://merchant.example/oauth/authorize",
  "token_endpoint": "https://merchant.example/oauth/token",
  "revocation_endpoint": "https://merchant.example/oauth/revoke",
  "jwks_uri": "https://merchant.example/.well-known/jwks.json",
  "scopes_supported": [
    "dev.ucp.shopping.order:read",
    "dev.ucp.shopping.order:manage",
    "dev.ucp.shopping.cart:manage"
  ],
  "response_types_supported": ["code"],
  "grant_types_supported": ["authorization_code"],
  "code_challenge_methods_supported": ["S256"]
}
```

2. Enforce PKCE S256 for all exchanges; reject plain PKCE

3. Return `iss` parameter in authorization responses to prevent mix-up attacks

4. Validate Bearer tokens in every authenticated request

5. Implement token revocation endpoint for unlinking

### Scope Model

**Format:** `{capability}:{permission}`

Examples:
- `dev.ucp.shopping.order:read` — Read user's orders
- `dev.ucp.shopping.order:manage` — Modify orders (cancellations, returns)
- `dev.ucp.shopping.cart:manage` — Manage user's cart

**Merchant declares required scopes:**

```json
{
  "config": {
    "scopes": {
      "dev.ucp.shopping.order:read": {
        "description": "Access to order history"
      },
      "dev.ucp.shopping.order:manage": {
        "description": "Ability to cancel and modify orders"
      }
    }
  }
}
```

**Semantics:** Scopes listed in `config.scopes` indicate hard authentication gates. Operations without listed scopes operate at:
- **Public tier:** No authentication required
- **Agent-authenticated tier:** Platform authenticated via API key or HTTP Message Signatures
- **User-authenticated tier:** User identity token required (and scopes verified)

### Authorization Flow

**Step 1: Platform initiates authorization**

```
GET https://merchant.example/oauth/authorize
  ?client_id=platform-client-id
  &redirect_uri=https://platform.example/oauth/callback
  &response_type=code
  &state=random_state_value
  &scope=dev.ucp.shopping.order:read+dev.ucp.shopping.order:manage
  &code_challenge=EXAMPLE_code_challenge
  &code_challenge_method=S256
```

**Step 2: User authenticates and consents**
- Merchant displays login page
- User enters credentials
- Merchant displays consent screen showing requested scopes
- User clicks "Allow"

**Step 3: Merchant issues authorization code**

```
Redirect to: https://platform.example/oauth/callback
  ?code=authorization_code_value
  &state=random_state_value
  &iss=https://merchant.example
```

**Step 4: Platform exchanges code for token**

```
POST https://merchant.example/oauth/token
Content-Type: application/x-www-form-urlencoded

grant_type=authorization_code
&code=authorization_code_value
&client_id=platform-client-id
&client_secret=platform-client-secret
&redirect_uri=https://platform.example/oauth/callback
&code_verifier=EXAMPLE_code_verifier
```

**Response:**

```json
{
  "access_token": "user_access_token_value",
  "token_type": "Bearer",
  "expires_in": 3600,
  "scope": "dev.ucp.shopping.order:read dev.ucp.shopping.order:manage"
}
```

### Error Handling: Authentication Tiers

**401 `identity_required`**: Returned when no valid token is presented for protected operation

```
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer realm="Merchant UCP", error="invalid_token"
Content-Type: application/json

{
  "error": "identity_required",
  "error_description": "User authentication required"
}
```

**403 `insufficient_scope`**: Returned when token lacks required scopes

```
HTTP/1.1 403 Forbidden
WWW-Authenticate: Bearer realm="Merchant UCP", \
  error="insufficient_scope", \
  scope="dev.ucp.shopping.order:read dev.ucp.shopping.order:manage"

{
  "error": "insufficient_scope",
  "error_description": "Request requires additional scopes",
  "required_scopes": [
    "dev.ucp.shopping.order:read",
    "dev.ucp.shopping.order:manage"
  ]
}
```

### Scope Validation

**On every authenticated request:**

```
verify_token(request, required_scopes):
  1. Extract Authorization header: "Bearer <token>"
  2. Validate token:
     - Not expired
     - Signed with merchant's key (jwks_uri)
     - Issued by merchant (iss claim matches)
  3. Extract scopes from token
  4. Check all required_scopes are present in token
  5. If missing: return 403 insufficient_scope with required list
```

### Token Revocation

**When user unlinks account:**

```
POST https://merchant.example/oauth/revoke
Content-Type: application/x-www-form-urlencoded

token=user_access_token_value
&token_type_hint=access_token
```

**Response:**

```
HTTP/1.1 200 OK
```

After revocation, the token becomes invalid. Subsequent requests with that token return 401.

### Advanced: Accelerated IdP

**Scenario:** Platform holds upstream token from trusted provider (e.g., Google)

**Flow:**

```
Platform uses JWT Bearer assertion grant:
POST https://merchant.example/oauth/token
Content-Type: application/x-www-form-urlencoded

grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer
&assertion=<jwt_from_upstream_provider>
&requested_scope=dev.ucp.shopping.order:read
```

**Benefits:** Eliminates browser redirect; maintains merchant-issued token under proper audience and scope constraints.

**Implication for our merchant:** Publish OAuth endpoints at `/.well-known/oauth-authorization-server`. Require PKCE S256. Return `iss` in auth responses. Validate scopes on every request. Implement token revocation.

---

## 8. Security Best Practices

**Specification Source:** https://ucp.dev/documentation/schema-authoring/ | https://ucp.dev/specification/overview

### Credential Flow Direction (CRITICAL)

**Rule: Credentials flow ONLY from platform → business**

- Platform collects payment instrument credentials from buyer
- Platform tokenizes credentials with PSP (Payment Service Provider)
- Platform sends opaque token to merchant
- **Merchant MUST NEVER:**
  - Receive raw payment credentials (PAN, CVV, private keys)
  - Store or handle raw credentials
  - Send raw credentials back to platform

This architecture minimizes PCI DSS scope for merchants.

### What MUST NOT Appear in Responses

- Raw payment credentials (PANs, CVVs, private keys)
- Raw authentication tokens (OAuth refresh tokens, API keys)
- Internal merchant IDs or database identifiers
- Customer personally identifiable information (PII) beyond what platform provided
- System error details (stack traces, SQL errors)

### Namespace Authority Validation

**Requirement:** Validate that capability specifications originate from the declared authority.

```
Capability name: dev.ucp.shopping.checkout
Reverse domain: dev.ucp → ucp.dev
Declared spec URL: https://ucp.dev/specification/checkout
Actual origin: ucp.dev ✓ VALID

Capability name: com.stripe.payment_method
Reverse domain: com.stripe → stripe.com
Declared spec URL: https://attacker.example/stripe-spec
Actual origin: attacker.example ✗ INVALID
```

**Implementation:**

```
validate_capability_authority(name, spec_url):
  1. Extract reverse-domain from name: "dev.ucp.shopping.checkout" → "dev.ucp"
  2. Reverse to domain: "dev.ucp" → "ucp.dev"
  3. Parse origin from spec_url: "https://ucp.dev/..." → "ucp.dev"
  4. Require: origin == reversed_domain
     Reject if not matching
```

### Error/Abuse Handling

**Signature Verification Failures:**
- Return 401 with appropriate error code
- DO NOT include additional debugging information
- Log incident for monitoring
- Consider rate-limiting repeated failures from same source

**Invalid Requests:**
- Return HTTP 400 (Bad Request) for malformed JSON, missing required fields
- Return HTTP 422 (Unprocessable Entity) for semantic validation failures
- Include error code in response body
- Do NOT expose internal schema validation rules

**Rate Limiting:**
- Implement per-source rate limits on sensitive operations (Complete Checkout)
- Return HTTP 429 Too Many Requests with `Retry-After` header
- Return `rate_limit_exceeded` error code

**Fraud Signals:**
- Accept platform-provided signals in `context.signals`
- Use in risk assessment, not as blocking criteria
- Allow platform to provide fraud metadata (device fingerprints, velocity checks)
- Merchant decides accept/reject; platform decides UX

### Versioning & Content Negotiation

**Protocol versioning:**
- Merchant advertises `ucp.version` (current version)
- Optionally includes `supported_versions` mapping older versions
- Platform selects highest common version

**Capability versioning:**
- Independent from protocol version
- Date-based (YYYY-MM-DD)
- Selected via intersection algorithm (section 4)

**Backward compatibility:**
- New capabilities must not break old clients
- Breaking changes increment capability version
- Old versions remain in `supported_versions` mapping

### Required vs. Recommended

**REQUIRED (MUST implement):**
- RFC 9421 signature verification on requests
- Capability negotiation algorithm (return active capabilities in responses)
- Idempotency-Key support for Create/Complete/Cancel
- Error message structure (type, code, severity, path)
- Payment handler isolation (never store raw credentials)
- PKCE S256 validation in OAuth flow
- Namespace authority validation

**RECOMMENDED (SHOULD implement):**
- Response signing (RFC 9421)
- Webhook signatures for outbound notifications
- HTTPS with TLS 1.3 minimum
- OAuth token rotation
- Content-Digest verification on received responses
- Profile caching with TTL
- Rate limiting

**IMPLEMENTED (pragmatic scope, Travel Guild submission):**
- AP2 Mandates: W3C-VC 2.0 two-tier mandate envelope + SIMULATED settlement (`alipay_sim.go`); full SD-JWT/JCS/PSP chain is deferred (DESIGN §13).

**OPTIONAL (MAY implement):**
- Embedded checkout (iframe delivery)
- MCP transport (REST only is sufficient)
- Split payments, loyalty, fulfillment extensions

**Implication for our merchant:** Implement all REQUIRED items. Validate namespace authority. Never store raw credentials. Return appropriate error codes. Support PKCE S256 OAuth flow.

---

## 9. Summary & Implications for Our Merchant

| Component | Our Merchant Implication | Priority |
|-----------|-------------------------|----------|
| **Manifest** | Publish `/.well-known/ucp` with services, capabilities, signing_keys in exact JWK format | CRITICAL |
| **HTTP Message Signatures** | Verify RFC 9421 signatures on platform requests; validate body digest; cache platform profiles | CRITICAL |
| **Signature Verification** | Reconstruct signature base per RFC 9421; verify ECDSA using P-256 public keys; handle key rotation | CRITICAL |
| **Agent Profile** | Ensure profile URL in UCP-Agent header matches fetched profile origin | HIGH |
| **Capability Negotiation** | Include `ucp.capabilities` in responses; implement intersection algorithm; prune extensions | HIGH |
| **Checkout REST** | Implement POST /checkout-sessions, GET/PUT /checkout-sessions/{id}, POST /checkout-sessions/{id}/complete, POST /checkout-sessions/{id}/cancel | CRITICAL |
| **Checkout Status** | Manage 6-status lifecycle (incomplete, requires_escalation, ready_for_complete, complete_in_progress, completed, canceled) | CRITICAL |
| **Error Handling** | Return errors with type/code/severity/path; map severity to platform action | HIGH |
| **Payment Instruments** | Receive opaque tokens; NEVER store raw credentials; validate handler_id exists | CRITICAL |
| **Eligibility** | Verify all claims before completion; return eligibility_invalid error if verification fails | HIGH |
| **AP2 Mandate** | Sign checkout with JWS detached content (RFC 8785 canonicalization); verify mandates from platform | HIGH (if supporting autonomous checkout) |
| **OAuth Identity Linking** | Publish `/.well-known/oauth-authorization-server`; enforce PKCE S256; return `iss` in auth responses | MEDIUM (if supporting user-authenticated ops) |
| **Idempotency** | Implement 24–48 hour caching on Idempotency-Key for Create/Complete/Cancel | HIGH |
| **Totals Rendering** | Render all totals in order provided; no reordering or filtering | CRITICAL |

**Quick build roadmap:**

1. **Phase 1 (IMMEDIATE):**
   - [ ] Publish manifest at `/.well-known/ucp` with signing keys
   - [ ] Implement RFC 9421 signature verification on requests
   - [ ] Build Create/Get/Update/Complete/Cancel checkout REST endpoints
   - [ ] Implement 6-status lifecycle with error messages
   - [ ] Handle payment instruments (receive tokens, don't store raw data)

2. **Phase 2 (NEXT):**
   - [ ] Implement capability negotiation algorithm
   - [ ] Add Idempotency-Key support (24–48 hour caching)
   - [ ] Implement eligibility verification before completion
   - [ ] Validate totals rendering contract
   - [ ] Add namespace authority validation

3. **Phase 3 (OPTIONAL):**
   - [x] AP2 mandate (pragmatic): W3C-VC envelope + raw ECDSA, SIMULATED settlement — IMPLEMENTED (#57)
   - [ ] Full SD-JWT/JCS/PSP payment-mandate chain (deferred, DESIGN §13 scope)
   - [ ] Implement OAuth endpoints for identity linking
   - [ ] Add response signing (RFC 9421)
   - [ ] Implement webhook notifications with signatures
   - [ ] Add MCP transport binding

---

## Auth-Layer Build Checklist

### REQUIRED (Blocking merchant go-live)

**Discovery & Manifest:**
- [ ] Serve JSON at `/.well-known/ucp` with `ucp.version`, `ucp.services`, `ucp.capabilities`, `ucp.payment_handlers`, `signing_keys`
- [ ] Include P-256 ECDSA public keys in JWK format with `kid`, `kty`, `crv`, `x`, `y`, `use`, `alg` fields
- [ ] Validate all capability `schema` URLs point to schemas with matching `name` and `version` embedded

**RFC 9421 Signature Verification:**
- [ ] Parse `Signature-Input` header; extract `keyid`, `created`, `alg`, component list
- [ ] Fetch platform's profile from `UCP-Agent` header URL; cache with 24-hour TTL
- [ ] Locate public key by `kid` in signing_keys array
- [ ] Verify `Content-Digest` matches SHA-256(raw body bytes)
- [ ] Reconstruct signature base per RFC 9421; verify ECDSA signature (fixed-width r||s encoding)
- [ ] Return 401 with `signature_invalid`, `key_not_found`, or `digest_mismatch` error codes on failure
- [ ] Verify profile origin matches UCP-Agent header domain

**REST Checkout Operations:**
- [ ] POST `/checkout-sessions` — Create with line_items, buyer, context, signals, payment
- [ ] GET `/checkout-sessions/{id}` — Retrieve current state
- [ ] PUT `/checkout-sessions/{id}` — Full resource replacement
- [ ] POST `/checkout-sessions/{id}/complete` — Finalize order; verify eligibility
- [ ] POST `/checkout-sessions/{id}/cancel` — Terminate session
- [ ] All endpoints require `UCP-Agent` header; `Complete` and `Cancel` require `Idempotency-Key`

**Checkout Lifecycle:**
- [ ] Implement 6 status values: incomplete, requires_escalation, ready_for_complete, complete_in_progress, completed, canceled
- [ ] Return errors with type/code/severity/path/content fields
- [ ] Map error severity to platform action: recoverable (update + retry), requires_buyer_input/review (escalate), unrecoverable (new checkout)
- [ ] Include `continue_url` (absolute HTTPS) when status is `requires_escalation`
- [ ] Populate `ucp.capabilities` with active capabilities in every response

**Payment & Eligibility:**
- [ ] Receive opaque payment.instruments; NEVER store raw PAN/CVV/keys
- [ ] Validate payment handler_id exists in ucp.payment_handlers
- [ ] Verify all context.eligibility claims before Complete Checkout
- [ ] Return error code `eligibility_invalid` with severity `recoverable` if verification fails

**Idempotency:**
- [ ] Cache Create/Complete/Cancel responses by Idempotency-Key header for 24–48 hours
- [ ] Return cached response if duplicate request received
- [ ] Require Idempotency-Key for Create/Complete/Cancel; optional for Get/Update

**Totals Rendering Contract:**
- [ ] Render all totals entries in order provided by merchant
- [ ] NEVER reorder, filter, aggregate, or apply display logic
- [ ] Preserve sign convention (positive = charge, negative = credit)
- [ ] Ensure sum(all_totals) equals total amount

### HIGH PRIORITY (Required for Phase 1 completion)

**Namespace Authority:**
- [ ] Validate that capability spec URLs originate from declared reverse-domain authority
- [ ] Reject capabilities with mismatched authorities

**Message Structure:**
- [ ] Return errors with all fields: type, code, severity, content, path (JSONPath)
- [ ] Return warnings with: type, code, content, presentation (notice or disclosure)
- [ ] Render disclosure warnings non-dismissible in proximity to referenced component

**API Response Structure:**
- [ ] Wrap checkout in `ucp` metadata object with version, status, capabilities, payment_handlers
- [ ] Return HTTP 200 for all checkout responses (including business errors)
- [ ] Return HTTP 4xx/5xx only for protocol errors (auth, format, transport)

### MEDIUM PRIORITY (Phase 2)

**Capability Negotiation:**
- [ ] Implement intersection algorithm (match by name, select version, prune extensions)
- [ ] Return `ucp.capabilities` with active set in every response
- [ ] Observe platform's profile to understand its capabilities

**OAuth Identity Linking (if supporting user-authenticated operations):**
- [ ] Publish `/.well-known/oauth-authorization-server` per RFC 8414
- [ ] Implement `/oauth/authorize`, `/oauth/token`, `/oauth/revoke` endpoints
- [ ] Enforce PKCE S256; reject plain PKCE
- [ ] Return `iss` parameter in authorization response
- [ ] Validate scopes on every authenticated request
- [ ] Define `config.scopes` for operations requiring user identity

**Response Signing (Recommended):**
- [ ] Sign checkout responses with Signature-Input, Signature, Content-Digest headers
- [ ] Use merchant's private key to sign; platform verifies with signing_keys

### LOW PRIORITY (Phase 3 / Optional)

**AP2 Mandate Support (IMPLEMENTED — pragmatic scope):**
- [x] W3C-VC 2.0 two-tier mandate envelope (`checkoutMandateVC` + simulated `PaymentMandate`/Alipay rail) — task #57
- [x] Signed budget/expiry consent verified server-side for L3 autonomous `complete_checkout`; settlement SIMULATED (`Simulated:true`, `alipay_sim.go`)
- [ ] Full SD-JWT/JCS/PSP chain — deferred per DESIGN §13 (needs business account for real Alipay rail)
- [ ] Canonicalize checkout using RFC 8785 (JSON Canonicalization Scheme) — deferred per DESIGN §13

**MCP Transport Binding:**
- [ ] Expose tools: create_checkout, get_checkout, update_checkout, complete_checkout, cancel_checkout
- [ ] Include `meta.ucp-agent.profile` on every request
- [ ] Include `meta.idempotency-key` on Create/Complete/Cancel
- [ ] Support RFC 9421 signing over JSON-RPC bodies

**Webhooks:**
- [ ] POST completed orders to platform's webhook URL (if provided)
- [ ] Sign webhooks with Signature-Input, Signature, Content-Digest
- [ ] Include Webhook-Timestamp, Webhook-Id headers (Standard Webhooks)
- [ ] Platform verifies webhook signature using merchant's signing_keys

---

## References

| Section | Source URL |
|---------|-----------|
| 1. Manifest | https://ucp.dev/specification/overview, https://ucp.dev/latest/specification/checkout-rest/ |
| 2. HTTP Message Signatures | https://ucp.dev/specification/signatures/, RFC 9421, RFC 9530, RFC 7517 |
| 3. Agent Profile | https://ucp.dev/latest/specification/checkout/, https://ucp.dev/documentation/core-concepts/ |
| 4. Capability Negotiation | https://ucp.dev/documentation/core-concepts/, https://ucp.dev/specification/overview |
| 5. Checkout Capability | https://ucp.dev/specification/checkout/, https://ucp.dev/specification/checkout-rest/, https://ucp.dev/specification/checkout-mcp/ |
| 6. AP2 Mandate | https://ucp.dev/specification/ap2-mandates/, RFC 8785 (JSON Canonicalization), RFC 7515 (JWS) |
| 7. Identity Linking | https://ucp.dev/specification/identity-linking/, RFC 8414 (OAuth Auto-Discovery), RFC 6750 (Bearer Token) |
| 8. Security Best Practices | https://ucp.dev/documentation/schema-authoring/, https://ucp.dev/specification/overview |
| GitHub Repository | https://github.com/Universal-Commerce-Protocol/ucp |
| Samples & Conformance | https://github.com/Universal-Commerce-Protocol/samples, https://github.com/Universal-Commerce-Protocol/conformance |

---

**Document Status:** Protocol spec extracted 2026-04-08; implementation addenda for AP2 mandate (W3C-VC pragmatic, task #57) added 2026-06-29. Build-roadmap checkboxes reflect current state as of that date.  
**Intended Use:** Implementation guide for Go stdlib UCP merchant with manifest, MCP tools, checkout operations, budget/HITL support, and HTTP signature verification.
