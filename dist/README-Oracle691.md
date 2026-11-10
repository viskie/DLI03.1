# Oracle 961 portal at /Oracle691

Local implementation based on the supplied dashboard reference. Python / Flask backend, encrypted file storage, password accounts, Google OAuth integration, Razorpay one-time checkout, and persisted payment receipts. Marketplace, trading, wallet custody and token issuance are explicitly previews, not live functions.

## Run locally

From the workspace parent, run `.venv/bin/python portal/app.py`, then open http://127.0.0.1:8000/Oracle691 . For a fresh environment, install `requirements.txt` into a Python virtual environment and run `python app.py` from this folder. The server binds to loopback only.

## Accounts and storage

Local files live in the ignored `instance/` directory. The account/order/payment document is authenticated and encrypted with Fernet. Passwords use Werkzeug scrypt hashes. A file lock serializes transactions and writes use atomic replacement. Keys are generated on first local startup with owner-only permissions. Production should supply `ORACLE_DATA_KEY` and `FLASK_SECRET_KEY` from secret configuration, with the encryption key stored separately from encrypted data. Preserve and securely back up both data and keys; losing a key prevents recovery.

This store supports a single host with a persistent filesystem. It is not a distributed storage engine. No SQLite is used. Session cookies are signed, HttpOnly, SameSite Lax, with CSRF tokens on mutations. Set `ORACLE_HTTPS=true` behind HTTPS. Never expose Flask's development server publicly. Registration does not verify email and does not send email. Email verification and self-service password recovery are not implemented.

## Private local settings

Fill `.env.oracle.local` in this folder with provider credentials and restart the local Python app. This ignored file is loaded without overriding environment variables. Keep secrets out of chat and version control. A blank template is supplied in `.env.example`.

## Social registration and sign-in

Google, Facebook and LinkedIn buttons appear on both email sign-in and registration. A first successful provider login creates an account; a returning provider identity reuses it. Accounts are never linked solely by matching email addresses. Provider credentials are server-side environment variables. OAuth state and OIDC claims are validated by Authlib. Google and LinkedIn require verified email claims; Facebook requires an email returned by the authenticated profile endpoint. Missing email access fails closed. No provider access tokens are persisted in account files.

- Google: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`; enable OAuth web client with `openid email profile`.
- Facebook: `FACEBOOK_CLIENT_ID` (Meta App ID), `FACEBOOK_CLIENT_SECRET`, `FACEBOOK_GRAPH_VERSION` as supported by your Meta app; enable Facebook Login with `email public_profile`.
- LinkedIn: `LINKEDIN_CLIENT_ID`, `LINKEDIN_CLIENT_SECRET`; enable the Sign In with LinkedIn using OpenID Connect product with `openid profile email`.

Register these local redirect URIs exactly:

- `http://127.0.0.1:8000/auth/google/callback`
- `http://127.0.0.1:8000/auth/facebook/callback`
- `http://127.0.0.1:8000/auth/linkedin/callback`

Use each provider's `*_REDIRECT_URI` setting for the final HTTPS domain. Facebook may require HTTPS and an allowed development/test app configuration. Add the deployed domain and any required privacy, terms, or data-deletion URLs in the provider dashboard. LinkedIn sign-in is authentication, not investor identity verification.

## Two plans and Razorpay

1. **Investment-linked subscription tokens**: USD 300–10,000.
2. **Tradable prepaid subscriptions Plus**: USD 5,000–50,000, combining future API service/resale rights with Plan 1 investment-linked benefits.

The slider and exact numeric input accept whole-dollar amounts. The selected plan and amount survive registration/sign-in, including OAuth redirects in the same tab. The server independently validates the plan, range, integer type and currency subunits. Investment allocations, API quantities, duration, profit-sharing, redemption, transfer and eligibility terms are not established by the amount selector. Token issuance, trading and automatic investment distributions are not implemented.

Configure `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET` and `RAZORPAY_WEBHOOK_SECRET` in the process environment, then set `ORACLE_CHECKOUT_ENABLED=true`. Start with test keys. The framework uses the selected amount in USD subunits to create an order on the server. Confirm that your Razorpay account supports USD, the intended transaction amounts and the actual business offering before live collection. Razorpay's published terms restrict securities, crypto products and some resalable-credit activities; ordinary onboarding does not automatically waive prohibited-product terms.

Orders are stored with their account owner. Callback verification checks the signature, owner, payment amount, currency and captured status through Razorpay's API. Captures create idempotent encrypted receipts. Configure `payment.captured` at `/api/razorpay/webhook`; webhook signatures are checked before receipt updates. Authorized but uncaptured payments are pending. Payment records do not mint tokens or grant investment rights. Recurring billing is not enabled. Actual provider flows need credentials before live external testing.

## O369 proof-of-concept daily contracts

The 1-day, 3-day and 9-day contracts are generated from the signed O369 export.
When `DLI_ORACLE_CONTRACT_ISSUANCE_MODE=poc-research` is set on the `.info`
production portal, the catalogue publishes the available research-ranked list
without describing it as trade-qualified. Contracts expose
`proofOfConcept=true`, `tradeQualified=false`, `executionEligible=false`, and
preserve row-level trade blockers. “Available” means research-list access is
available; it does not authorize execution or promise a return.

The current contract caps are 30, 100 and 300 predictions for the 1-, 3- and
9-day products. Each contract selects the available ranked predictions whose
`targetAtMs` falls inside its access window, up to that cap. Private batches are stored separately by contract ID inside the
encrypted artifact; public responses expose counts and hashes but remove both
`predictionBatch` and `predictionBatches`.

Plan 3 contract price is the rounded-up full-batch exchange-capital estimate,
subject to a USD 10,000 maximum. The required exchange capital remains separate
and subscriber-controlled, so the admin panel discloses purchase price,
exchange capital and combined known cash independently. Bot execution remains
disabled until a bot and its commercial/legal operating model are defined.

### Latest-nine verified contract replay

The hourly publisher keeps encrypted, hash-chained status archives for 90 days.
For the admin-only contract report, it selects the latest catalogue revision
for each UTC issue day and duration, then matches each batch row to an
authenticated actualized outcome by market, origin timestamp and target
timestamp. It returns the nine newest batches with at least one verified row.
Incomplete batches are explicitly labelled partial. Unmatched outcomes are
excluded and counted; they are never assigned fabricated prices.

Gross directional P&L uses one recorded per-trade budget for every prediction,
entry at the recorded spot price, and exit at the authenticated actual target
price. Net P&L subtracts the entry fee, exit fee and execution-cost reserve
stored with that contract. Historical contracts whose stored cost inputs are
zero are visibly marked: their net equals gross and is not exchange-realistic.
Short funding/borrow, taxes and bot hosting remain excluded. The result is a
counterfactual historical simulation, not executed profit or a return promise.

Implementation: `_matured_contract_simulations` in `status_generator.py` builds
the report, `app.py` keeps it out of the public status endpoint, and the
administrator selects reports under **Plan 3 actualization**. Correct/wrong
counts come from Prediction-versus-Reality, while profitable/loss-making counts
come from calculated net P&L. Reports refresh after each hourly publication.

The contract engine is `contracts.py`, the storefront is
`public/Oracle691/oracle.js`, and regression coverage is in
`tests/test_status.py`. After deploying the committed `dist` tree to Vercel,
start `oracle961-status.service` on the laptop to publish a fresh encrypted
catalogue and verify `/api/oracle961/contracts`.

## Verification

From the workspace parent: `.venv/bin/python -m unittest discover -s portal/tests -v`. Tests use isolated temporary storage and mock Razorpay. They cover password login, profile edits, encryption at rest, storage reopening, CSRF, private-file protection, server-side pricing, order ownership, capture validation and idempotent webhooks.

## Deployment status

The portal is deployed at `https://www.delta-indices.net/Oracle691`. Vercel
serves the Python portal while durable encrypted status is published to the
configured Blob store by the laptop's hourly `oracle961-status.service`. The
publisher's local encrypted archives are the evidence source for mature-contract
simulation and must be retained with their encryption key.

## Administration

Use the portal’s existing Sign in form with username `admin` and password `pass` to open `/Oracle691/admin`. This static fallback only authenticates loopback requests with a localhost/127.0.0.1 host and no forwarding header. It is not a public production login. Change the password in My profile; a new hashed password replaces the fallback. Registered administrator roles can also sign in through the ordinary account flow.

The dashboard includes member profile edits, member/admin roles, suspension/restoration, the administrator profile, captured-payment reports, user and audit CSV exports, and first-party web analytics. Suspension and role checks are evaluated on each protected request. CSV values are protected against spreadsheet formula injection. Reports use actual stored records, not demonstration data.

Providers & secrets allows Google, Facebook, LinkedIn and Razorpay credentials, callback URLs and the checkout toggle to be saved in encrypted account storage. Existing secrets are masked and never returned to the browser. Blank fields preserve the saved secret, while Clear removes it. Settings apply immediately and are restored on startup, overriding the corresponding environment defaults. The app does not offer an endpoint to reveal a secret. Account encryption/session signing keys cannot be edited through this UI.

Analytics aggregates portal page views, random browser-session identifiers and defined plan/registration/checkout events for up to 90 days. It does not store IP addresses or query strings, and browser sessions are not unique-person counts. Admin-page views are excluded. A local browser preview may contribute to the recorded portal traffic.

The Setup guide inside the dashboard includes steps, callback URLs, scopes and documentation links for obtaining all third-party credentials. No actual provider keys have been generated or entered. External sign-in and payment tests remain pending those credentials.

Saving complete OAuth provider settings activates that provider immediately. Saving all three Razorpay credentials automatically enables checkout, unless the administrator explicitly disabled it. An explicit disable persists until enabled or cleared. Service status indicates configured credentials; an actual external sign-in/payment is still required to verify them.
