# Design: the desktop's Local / Cloud switch and the group mode

Status: **proposed, for the owner's approval.** No code is written yet.
Tracker row R691 ("Switch the desktop app between a local backend and a
cloud (Fleet) backend"). The work item is the `docs/TODO.md` entry "A person
uses their cloud VM from the desktop app, not only the local backend",
agreed on 2026-11-08 and written down in PR #15.

This doc is in igni-core because the TODO, `CLOUD.md` and `SECURITY.md` are
here. Most of the code lands in igni-app and igni-server, so each of those
PRs links back to this file rather than copying it.

Every "today" statement below was read from the code on 2026-11-20:

- igni-core origin/main `51aa1b89`;
- igni-server origin/main `997ba78`;
- igni-app origin/main;
- igni-sdk origin/main `80e29ee`;
- igni__cloudvm `10a8e5b` (crate `crates/gateway`).

Paths are given relative to each repo.

---

## 0. Decisions at a glance

| # | Question | Decision |
|---|---|---|
| D1 | Discovery | igni server gets a new `GET /v1/me/cloud`. It answers **entitlement**: is there a cloud igni for this person, the gateway URL, the group mode and who owns the events. The gateway's existing `GET /v1/me` gains the **live VM state** (asleep, running, signed in). igni server never asks the gateway for state. |
| D2 | Auth hand-off | **The token never enters the webview.** The Tauri shell runs a loopback **relay**: windows connect to it exactly as they connect to a local backend (per-launch token, Origin check), and the relay dials `wss://<gateway>/v1/connect` with `Authorization: Bearer <session>` read from `~/.igni/credentials.json`. "Refresh" means re-dialling with the current token. The CSP stays loopback-only. |
| D3 | Switch UX / state | One app-wide choice, owned by the shell and remembered per account. It is a two-way control, *This computer · Cloud*, in the account menu, with a backend chip in the status line. Flipping re-points every window, the same way a backend restart does today. The local backend keeps running. |
| D4 | Group mode | A first-class column, `org_groups.mode ∈ {local, cloud, local_and_cloud}`, defaulting to `local_and_cloud` (today's behaviour). It is carried as a top-level `mode` in the signed group pack. It is enforced in four places:<br>• the shell, offline, from the verified pack;<br>• igni-core's run gate, offline;<br>• igni server: the producer feed marks a local-only person inactive, and the `vm-sign-in` grant refuses them;<br>• the gateway, which refuses and kicks inactive producers. |
| D5 | Cloud mode, phase 1 | Chat works: sessions, runs, approvals, models, attachments (uploaded as bytes). Hidden: extensions, the folder picker, New Window's folder choice, "open in editor" and "reveal" for paths, the hooks panel's file links. The cloud layer also refuses `pick_dir_native` on its own side. |
| D6 | Events | **igni server picks one owner per person** (`local` or `cloud`), so the two sides never race for the lease. Owner by mode: `local` → local; `cloud` → cloud; `local_and_cloud` → the person's last switch, stored server-side, `local` until they first switch. A desktop pull while the owner is `cloud` gets `409`. A gateway pull while the owner is `local` gets nothing. |
| D7 | Errors | Each error state gets one shell state and one banner. Waking is driven by the client, because the gateway sends nothing while it wakes a VM. `needs_sign_in` gets a **Sign in** button (the shell calls `POST /v1/sign-in`). An unreachable gateway retries with backoff and offers a way back to local. A mode change applies live (a kick, or a drain then a switch). |
| D8 | Contracts | One schema, `cloud-discovery.schema.json`, lives in igni-sdk `contracts/`. igni-server, igni-client and the shell are tested against it. The pack gains `mode`. The feed gains `inactive_reason`. **The client protocol does not change.** |
| D9 | Security | The session token stays in Rust memory and on disk at 0600, where it already is. The relay accepts only the webview's origins plus its own per-launch token. The gateway gains an Origin allow-list (no browser origins by default). TLS is always verified. |
| D10 | Model contract | **No model-facing text changes.** None of this touches prompts, tools, injected blocks or result formats. One trap is named in §11 so nobody falls into it later. |
| D11 | Phasing | Five phases, one PR per repo per phase, merged igni-server → igni__cloudvm → igni-core → igni-sdk → igni-app. Phases 1–3 come to about **25 engineer-days** plus about **4 days** of live E2E (§13). |

---

## 1. What exists today

### 1.1 The desktop

- **One backend per app.** The shell's supervisor
  (`clients/tauri/src-tauri/src/backend/supervisor.rs:45-81`) does one of
  three things:
  - attaches to `<project>/.igni/backend.lock`;
  - refuses a backend that speaks another wire version;
  - spawns `python -m igni_core.backend --ws-port 0 --project-dir <dir>`
    and waits for `{"status":"ready","ws_port":N}`.

  There is no health check and no automatic restart. File › Restart
  Backend re-points every window.
- **How a window finds its backend.** The window URL carries it:
  `index.html?ws=ws%3A%2F%2F127.0.0.1%3A<port>&host=tauri&…#igni_ws_token=<t>`
  (`backend/conn.rs:67-84`). The web client offers the subprotocols
  `igni.v1, igni-token.<t>` (`clients/web/src/protocol/wsEndpoint.ts:37-63`).
  It keeps one WebSocket per window
  (`clients/web/src/chat/windowServices.ts:77-88`), and on every `connected`
  it re-binds and hydrates the window model
  (`chat/useSessionBinding.ts:49-52`, `chat/sessionSwitcher.ts:190-217`).
- **Sign-in lives in the local backend.** The `login` RPC runs the
  browser hand-off: portal `/cli-auth?port=N`, then
  `POST /v1/auth/exchange`. The result is a **30-day session token**
  (`token_use=session`, `aud` = the issuer, no refresh token) in
  `~/.igni/credentials.json`, mode 0600. The shell already reads that file
  for `sub` (`src/group_pack/local_state.rs:8-27`). No Tauri command hands
  any token to the webview.
- **The shell verifies the signed group pack itself.**
  - Where: `src/group_pack/mod.rs`, which checks RS256, `typ`, `iss`,
    `sub`, `version` and the pins.
  - What it reads: four settings, before any backend exists.
  - Parsing is lenient: unknown fields are ignored (`policy.rs`).
- **CSP is loopback-only.** `tauri.conf.json:16` names only loopback
  sources, and `tests/config_contracts.rs:79-146` fails if any source
  points off this machine.
- **The client id key.** It is `ember-code:client-id`, with `:<label>`
  appended for every window but `main` (`clients/web/src/clientState.ts:49,72-75`).
- **Nothing about cloud, Fleet or the gateway exists** in igni-app.

### 1.2 The gateway (igni__cloudvm `crates/gateway`)

- **The connect route.** `GET /v1/connect[?sign_in=1]` takes the bearer
  in one of two ways (`auth.rs:284-303`):
  - an `Authorization: Bearer` header;
  - **or** the subprotocol `igni.bearer.<t>`, sent next to `igni.v1`.

  It echoes only `igni.v1` (`api.rs:140`).
- **Token checks.** RS256 against igni server's JWKS, with `iss` = `aud` =
  the issuer and 30 s leeway. A person's token must be `token_use=session`,
  `kind=person`, with an `org` (`auth.rs:244-280`). **The desktop's
  session token is exactly that.**
- **Sign-in.** On a person's first connection, or with `?sign_in=1`, the
  gateway passes that same session token to igni in the VM
  (`vm_sign_in{session_token, organisation}`, `control.rs:100-125`). igni
  exchanges it for the VM credential.
- **While a VM wakes, the gateway sends nothing.** It buffers up to 256
  client messages, holds for at most 20 s, then closes with 1011. igni's
  `welcome` is the first frame the client sees.
- **Moving a session.** When a session moves between nodes, the client is
  **not** told to reconnect. The gateway reconnects behind the same socket
  (up to 3 times). It answers in-flight `rpc_request`s with an error, and
  igni sends a **new `welcome`** on the same socket.
- **Token expiry.** `exp` is checked only at the upgrade. Nothing closes a
  socket when its token expires.
- **Close codes:**
  - 1013: busy, retry;
  - 1008 with a reason: not an active producer, kicked by the feed, or a
    licence refusal;
  - 1011: anything else.
- **Origin.** No Origin check and no CORS anywhere.
- **Group.** `group` is read, returned by `GET /v1/me`, and decides
  nothing. Whether a person may wake comes from igni server's producer feed
  (`active` and `removed`, `waker.rs:161-181`).
- **Public routes.** `GET /v1/me` (producer, org, group, expires_at),
  `POST /v1/sign-in`, `POST /v1/commands`, `GET /healthz`. VM state lives
  in the gateway's own PostgreSQL (`gw_producers`). Only the mTLS admin
  route `/v1/admin/producers/{p}` shows it.
- **Relaying.** The relay is transparent: `host_model` and `caller` are
  not filtered there. igni in the VM refuses `caller`
  (`Layer.connections_speak_for_extensions() == False`).

### 1.3 igni server (igni-server)

- **The gateway URL.** `igni_gateway` is advertised in
  `/.well-known/openid-configuration` (`app/api/base_v1/endpoints/identity/discovery.py:46,71`)
  from `FLEET_GATEWAY_URL`, and left out when Fleet is not configured.
  Nothing reads it.
- **The group pack.**
  - Fetched with `GET /v1/portal/me/group/pack`; unchanged packs answer
    304 against an ETag.
  - Signed by `app/services/portal/group_pack_signature.py`: RS256 with the
    token key ring, `typ: igni-group-policy+jwt`.
  - Claims: `iss, sub, org, iat, version` (epoch ms), `token_use=group_policy`
    and `policy`. There is no `exp`, so the pack works offline.
  - Payload fields: `group_id, group_name, fetched_at, entries,
    default_model, theme`. The order of fields is part of the contract.
- **Groups.**
  - `org_groups` has no mode or cloud field.
  - A person is in exactly one group (`uq_org_group_members_user_one_group`).
  - The admin edits a group with `PATCH /v1/portal/groups/{id}`
    (`UpdateGroupRequest`), from the portal's `GroupDetailPage`, whose
    `SettingsTab` cards save on change.
  - Audit is per request (`app/middlewares/request_audit.py`).
- **Every active member is an active person producer**
  (`services/cloud/producer_kinds.py:39-69`). Nothing opts a person out of
  the cloud. The feed trigger `producer_feed_on_group` fires on
  `vm_profile_id` and `is_default`.
- **Events share one lease.** `/v1/me/events/pull` (desktop) and the
  gateway's platform pull take items from **one queue with one 2-minute
  lease**. `docs/service-identity.md:1500-1507` says it directly:
  "whichever of the gateway and the desktop pulls first has the event for
  two minutes".

### 1.4 igni-core and igni-sdk

- **Desktop event pull.** igni-core pulls `/v1/me/events` itself
  (`extensions/event_pull.py`) when `Layer.fetches_own_events()` is true.
  It is true on the desktop and false in the cloud layer.
- **What dedupes deliveries.** `delivered_events` is kept per database:
  SQLite on the desktop, PostgreSQL in the VM. So when both sides run,
  the same event can be delivered twice, once on each side.
- **What the pack verifier checks.** `core/config/group_policy/signature.py`
  checks `iss`, `sub` and `org`, and refuses rollback by `version`. The
  pack is cached in `<data_dir>/group-policy/`. `IGNI_GROUP_POLICY_REQUIRED`
  closes the `RunGate` until a pack verifies. `GroupPolicyPack` ignores
  unknown fields.
- **`welcome`** carries only `client_id`.
- **igni-client's `Fleet(url, auth=)`**
  - Sends the bearer as a header with subprotocol `igni.v1`.
  - Reads the `vm_credential` push: `needs_sign_in` raises `SignInRequired`
    and `needs_reissue` raises `ReissueRequired` (PR #38).
  - Has no `igni_gateway` discovery.
  - igni-sdk has no TypeScript gateway client.

---

## 2. Discovery (D1)

### 2.1 Contract: `GET /v1/me/cloud` on igni server

**Auth:** the person's session token. App, space and VM tokens get `403`,
because this endpoint describes a person's desktop.

**200 response:**

```json
{
  "cloud": "available",
  "reason": null,
  "gateway_url": "https://fleet.acme.example",
  "mode": "local_and_cloud",
  "events_owner": "local",
  "group": { "id": "7f3e…", "name": "Engineering" },
  "producer": "person:2b9c…"
}
```

| Field | Values | Meaning |
|---|---|---|
| `cloud` | `available` \| `unavailable` | Whether this person has a cloud igni they may use from the desktop |
| `reason` | `null` \| `fleet_not_configured` \| `group_local_only` \| `inactive` \| `not_licensed` | Why it is `unavailable`. `null` when available |
| `gateway_url` | https URL or `null` | The same value as `igni_gateway`. `null` when Fleet is not configured |
| `mode` | `local` \| `cloud` \| `local_and_cloud` | The group's mode. The same value the pack carries, sent here so the switch need not wait up to 5 minutes for the next pack poll |
| `events_owner` | `local` \| `cloud` | Who pulls this person's service events (§7) |
| `group` | `{id, name}` | For display |
| `producer` | string | The gateway's producer id, for logs and support |

**Rules:**

- **Additive only.** Clients ignore unknown fields. An unknown `cloud` or
  `mode` value counts as `unavailable` or `local`, which is the safe
  direction. `reason` is an open set: unknown values are shown as
  "unavailable".
- `Cache-Control: private, max-age=60` and an `ETag`.
- **When the shell asks:**
  - at launch;
  - after sign-in or sign-out (it watches `credentials.json`);
  - every 5 minutes while the app is focused;
  - and at once when the gateway closes with a 1008 reason.

**Companion: `PUT /v1/me/cloud/events-owner`**

- Body: `{"owner": "cloud" | "local"}`.
- Answers `200`, with the same body as `GET`.
- Answers `409 {"error":"mode_forbids"}` when the mode does not allow that
  owner.
- Audited.

### 2.2 Live state: the gateway's `GET /v1/me` gains `vm`

```json
{ "producer": "…", "org": "…", "group": "…", "expires_at": 1795000000,
  "vm": { "state": "asleep", "signed_in": true } }
```

- `state` is one of `none | asleep | waking | running`. It is read from
  `gw_producers`, for the caller's own producer only.
- **The route never wakes the VM.**
- `signed_in` is the gateway's flag. The authoritative credential state is
  still the `vm_credential` push from igni, which igni replays to any
  client that attaches late (`CLOUD.md`, VM credential).

### 2.3 Why this split, and what was rejected

- **Rejected: igni server reports VM state by asking the gateway.**
  - The gateway owns runtime state and does not report it back.
  - The only way in is its mTLS admin API, which would put an admin
    credential and a new failure mode into igni server's public request
    path.
  - The live state matters only when the person is about to use the
    gateway, and the client already holds a token the gateway accepts.

  The owner's wording was "igni server tells the app … the VM's state".
  This design splits that sentence: the server answers *whether*, the
  gateway answers *how it is right now*. → Open question Q1.
- **Rejected: discovery from `igni_gateway` and the pack alone, with no new
  endpoint.**
  - `igni_gateway` belongs to the server, not the person, so it cannot
    express `group_local_only` or `inactive`.
  - The pack does not carry `mode` until phase 3, and refreshes every
    5 minutes.
  - Neither one carries the events owner.

  `igni_gateway` keeps its job: it is how a program finds the gateway
  (igni-client discovery, §10).
- **Rejected: putting `gateway_url` in the signed pack.**
  - The gateway address is deployment config, not group policy.
  - Changing it would re-sign every pack.
  - The trust argument is the same either way: the URL comes over TLS from
    the same `api_url` the token already goes to.

---

## 3. Auth hand-off and refresh (D2)

### 3.1 Decision: a loopback relay in the shell

```
 window (webview)                   Tauri shell (Rust)                         Fleet gateway
 ws://127.0.0.1:<relay>  ───────▶  relay: checks Origin + igni-token.<t>
   subprotocols igni.v1,            reads ~/.igni/credentials.json   ──────▶  wss://gw/v1/connect[?sign_in=1]
   igni-token.<relay token>         Authorization: Bearer <session>            subprotocol igni.v1
                         ◀───────   frames passed through unchanged  ◀──────
```

- **Windows see a local backend.** The relay listens on `127.0.0.1` on a
  port the OS picks, with its own per-launch token (32 random bytes, like
  `backend/ws_token.rs`). It runs the same Origin allow-list as the backend:
  `tauri://localhost`, `http(s)://tauri.localhost` and loopback. Switching
  to the cloud means pointing windows at `ws=ws://127.0.0.1:<relay>`. The
  web client's connection code does not change, and the **CSP stays
  loopback-only**: `csp_is_set_and_names_no_host_off_this_machine` keeps
  passing as written.
- **Upstream.** There is one gateway connection per window connection, so
  each window keeps its own `client_id` and window model, exactly as it
  does locally.
  - TLS goes through `rustls`. rustls and ring are already in the build;
    the platform root store is added so enterprise CAs work.
  - `HTTPS_PROXY` and `NO_PROXY` are honoured.
  - The bearer goes in the `Authorization` header, not a subprotocol,
    because Rust can set headers.
- **Re-dialling is the refresh.**
  - The relay reads `credentials.json` **on every dial**, never caching
    the token beyond one connection.
  - It watches the file: a sign-in or sign-out closes every upstream, and
    the windows reconnect through their normal backoff.
  - Session tokens last 30 days and have no refresh token, so nothing
    needs renewing mid-connection. A dial answered `401` moves the shell to
    *needs desktop sign-in* (§8).
- **Sign-in stays where it is.** In phases 1–2 the local backend runs the
  existing `login` flow. In `cloud` mode (phase 3) a stripped-down
  **account-only** backend does it (§6.4).

### 3.2 Rejected alternatives

1. **Rejected: the webview connects straight to the gateway with
   `igni.bearer.<token>`.** This is what the TODO literally says, and what
   the gateway already supports for browsers.
   - The token the gateway accepts from a person is their **30-day session
     token**. It can act on igni server for 30 days and mint a VM
     credential through `vm_sign_in`. Handing it to JavaScript puts it
     within reach of any script in the webview, such as a markdown or
     preview injection.
   - It needs a Tauri command that returns the token.
   - It needs the CSP opened to an unknown `wss:` host, which also opens a
     path for exfiltration.

   The gateway keeps the subprotocol for a future **hosted web UI**, which
   must not use a 30-day token either (Q3). → Open question Q2, because
   this departs from the TODO's wording.
2. **Rejected: the local igni-core backend proxies to the gateway.**
   - It ties the cloud connection to a process that `cloud` mode does not
     run.
   - It mixes two standings on one socket.
   - It needs protocol work in igni-core for no gain.
3. **Rejected: a short-lived token minted for the webview.**
   - The gateway accepts only `token_use=session` with `aud` = the issuer.
     igni server could mint a short session token, but that is new server
     surface, and the relay makes it unnecessary for the desktop.
   - It remains the right answer for a hosted web UI (Q3).

---

## 4. The switch: UX and state model (D3)

### 4.1 UX

- **Where.** A two-way control, **This computer · Cloud**, at the top of
  the account menu (`components/AccountMenu.tsx`), which is the one menu
  that exists today: there is no Settings page. A **backend chip** in the
  status line (`components/StatusLine.tsx`, next to `GroupIndicator`)
  shows the active side and, in the cloud, its state: `Cloud`,
  `Cloud · waking…`, `Cloud · sign in`, `Cloud · offline`.
- **When it shows.** Only when `cloud == available` **and**
  `mode == local_and_cloud`.
  - In `cloud` mode the chip shows `Cloud` with no control.
  - In `local` mode, or with no cloud igni, nothing new appears.
- **It is app-wide.** Every window flips together. A window per side is
  phase 5.
- **It is remembered per account** in the shell's own state file, keyed
  by `iss|org|sub`. The next launch starts on the side last chosen if it
  is still allowed, and otherwise falls back to local.
- **Flipping to Cloud:**
  1. The shell checks `/v1/me/cloud`.
  2. It sends `PUT events-owner {cloud}`.
  3. It starts the relay.
  4. It re-points every window.
  5. The windows hydrate from the VM's igni.

  The **local backend keeps running**: a run in progress there finishes.
- **What the person sees afterwards.**
  - Local runs still going or **approvals waiting locally** show as a
    badge on the control ("1 waiting on this computer"). Clicking it
    switches back.
  - Sessions are per side: the cloud's list is the VM's. Nothing is
    copied between sides.
- **Flipping to This computer** is the reverse: `PUT events-owner {local}`,
  re-point the windows, keep the relay idle.

### 4.2 State model (owned by the shell, broadcast to windows)

The shell is the single source of truth for which side is active. It
pushes `igni-backend-state` (a Tauri event) to every window and answers
`backend_state()`.

```
          ┌──────────── switch(local) / mode→local / kick(group_local_only) ─────────────┐
          ▼                                                                              │
     ┌─────────┐ switch(cloud) ┌──────────┐ upgraded ┌────────┐ welcome ┌─────────────┐  │
     │  Local  │──────────────▶│  Dialing │─────────▶│ Waking │────────▶│ CloudReady  │──┤
     └─────────┘               └──────────┘          └────────┘         └─────────────┘  │
                                 │  │ 401                │ 1011/timeout   │  ▲  push      │
                                 │  └─▶ NeedsDesktopSignIn               │  │ needs_sign_in
                                 │ net error / 503 / 1013                ▼  │ / ready     │
                                 └────────▶ CloudUnreachable(backoff) ◀─ CloudNeedsSignIn ┘
                                            (retry 1s→30s, "use this computer")
```

| State | What the windows show |
|---|---|
| `Local` | The normal UI, as today |
| `Dialing` / `Waking` | The previous content stays, dimmed under a "Waking your cloud igni…" overlay. The overlay appears 800 ms after the socket opens with no `welcome` yet, so a running VM never flashes it. Typing is allowed: the gateway buffers. |
| `CloudReady` | The normal UI. Cloud-hidden features are hidden (§5) |
| `CloudNeedsSignIn` | A banner: "Your cloud igni needs you to sign in again", with **Sign in**. Chat input is disabled. |
| `NeedsDesktopSignIn` | The existing sign-in wall (`SignInWall.tsx`): the desktop's own session expired or was revoked |
| `CloudUnreachable` | A banner: "Can't reach your cloud igni. Retrying…", plus **Use this computer** when the mode allows |
| `Refused(reason)` | A banner naming the reason (`group_local_only`, `inactive`, `not_licensed`, licence), then an automatic return to `Local` when local is allowed |

### 4.3 Reconnect and resync

Re-pointing uses the mechanism Restart Backend already uses
(`bootstrap.rs:55-63,142-148`): each window gets `app_url(dir)` with the
new `ws=`. One web-client change is needed for the cloud:

- **A second `welcome` on the same socket means a resync.** After a node
  move the gateway reconnects behind the socket, and igni sends a new
  `welcome` with a new `client_id`.
  - Today `bind()` runs only on `connected`, so the window would keep a
    stale `connection_id` and no attached session.
  - `IgniClient` must emit `connected` again on any `welcome` after the
    first.
  - The in-flight RPC errors the gateway sends ("igni's connection was
    moved…") are shown as retryable, not as failures.

### 4.4 Per-backend client state (TODO item 5)

- **The new key.** `ember-code:client-id[:<label>]` becomes
  `igni:client-id:<backend>[:<label>]`, where `<backend>` is `local` or
  `cloud-<first 12 hex of sha256(gateway_url|org|sub)>`.
  - Local migrates once: read the old key, write the new one, keep the old
    one for a release.
  - Hashing the key means a second account or gateway never collides, and
    no address is written in clear to localStorage.
- **Other per-window localStorage** (drafts, view keys) gets the same
  prefix. A sweep of `localStorage` uses in `clients/web/src` is part of
  the phase 1 PR.
- **Server-side state needs nothing.** The window model is kept by each
  backend, so it follows the backend automatically.

---

## 5. Cloud mode in phase 1: what works, what is hidden (D5)

| Feature | In cloud mode | Why |
|---|---|---|
| Chat, sessions, runs, approvals, model switch, slash commands | **Works** | Plain client protocol, relayed |
| Attachments (composer upload, drag and drop) | **Works.** The bytes go through `upload_attachment{filename, content_base64}` and land on the VM | Already an upload, not a path. The 5 MiB composer limit holds |
| File pills / code pills: open in editor, reveal in folder | **Hidden.** The in-app preview is kept where the backend serves the content | The path is on the VM. `plugin:opener` would open a missing or wrong local file |
| Project folder picker, New Window's folder choice | **Hidden.** New Window opens on the VM's workspace | `pick_dir_native` would open a dialog on a VM without a display |
| Extensions (pages, indicators, list) | **Hidden** | The gateway relays the client protocol, and the cloud refuses `caller` (`SECURITY.md` §10, L541/L555). Phase 4 |
| Hooks panel's "open file" | **Hidden** | Local path |
| File › Restart Backend / Reinstall | **Kept, labelled "(this computer)"** | They act on the local backend, which keeps running |
| Account menu, Log out | **Works.** Log out signs the desktop out, which also ends the cloud connection (the relay sees `credentials.json` change) | One identity |
| Updater, theme, group indicator | Unchanged | Shell-side |
| Backend version chip | Shows the VM's igni version | See below |

**Version skew.**

- The VM's igni and the desktop's bundled igni-core can differ. The web
  client already tolerates additive protocol changes (zod, unknown frames
  dropped).
- Locally the shell refuses a backend that speaks another wire *major*.
  The relay cannot see a version before `welcome`, and `welcome` carries
  none.
- Phase 1 therefore relies on additive compatibility, and **does not add
  a version to `welcome`**, which would be a client-protocol change. If
  the owner wants a hard refusal, see Q6.

**Defence on igni-core's side.** The cloud layer refuses, as an RPC
error, the RPCs that make no sense on a VM: `pick_dir_native` and any
other that opens a native dialog. A client other than igni-app, or an old
one, then gets an answer instead of a hang. This is a small igni-core
change in `packages/igni-cloud`.

---

## 6. Group mode (D4)

### 6.1 Storage

**The column.**

- `org_groups.mode`: a text column checked to `local | cloud |
  local_and_cloud`, NOT NULL, default `local_and_cloud`.
- The default keeps today's behaviour: every member is a producer, and
  the desktop runs locally.
- It is a tenant migration (`alembic_tenant`).

**The trigger.** `producer_feed_on_group` must add `mode` to its
`UPDATE OF` list. Without it, the gateway never hears that a group
changed mode.

**The API and portal.**

- `UpdateGroupRequest` and the group read model gain `mode`, edited with
  the existing `PATCH /v1/portal/groups/{id}` (admin only).
- In the portal it is a new card in `components/group/SettingsTab.tsx`:
  "Where igni runs for this group", with three choices and one line each
  on what that means for people. The card shows a warning before saving a
  change that would kick people out of a running cloud session.
- The middleware audits the request. The handler adds
  `add_detail({"mode": {"from": …, "to": …}})` so the audit export shows
  the change.

**Rejected: a `settings` entry such as `{"desktop":{"mode":"cloud"}}`.**
The shell can already read settings entries, so it is tempting. But:

- entries are free-form JSON, merged across entries;
- server-side enforcement (the feed and the sign-in grant) would have to
  parse policy JSON;
- the portal would have no typed control.

### 6.2 Signing

- `mode` becomes a **top-level field of the signed pack payload**,
  appended after `theme`. Appending keeps the documented field order.
  Signing, keys and claims are otherwise unchanged
  (`group_pack_signature.py`), and the ETag covers it automatically
  because it hashes the payload.
- **Older clients ignore it.** igni-core's `GroupPolicyPack` and the
  shell's serde struct ignore unknown fields, so an old client treats
  every group as `local_and_cloud`. Server-side enforcement (§6.3) still
  holds for `local`. For `cloud`, an old desktop keeps running locally
  until it updates (Q5).
- **A pack without `mode`** (an older server) means `local_and_cloud`.

### 6.3 Enforcement points

| Where | `local` | `cloud` | `local_and_cloud` |
|---|---|---|---|
| **Portal / API** | `PUT events-owner {cloud}` → 409 | `PUT events-owner {local}` → 409 | Either owner |
| **igni server, producer feed** | The person's row is `active: false, inactive_reason: "group_local_only"`. **Not** `removed`: no erasure, and the VM's data is kept under the profile's idle-erasure rule (Q4) | `active: true` | `active: true` |
| **igni server, `vm-sign-in` grant and VM-credential renewal** | Refused (`invalid_grant`, reason `group_local_only`). This is defence in depth: a VM somehow still awake cannot renew | Allowed | Allowed |
| **Gateway** | A wake is refused with **1008** `group_local_only`. A live connection is **kicked** by the existing feed path (`gw_kick`) within the feed's long-poll latency (seconds). `POST /v1/sign-in` → 403 with the same reason | — | — |
| **Shell (offline, from the verified pack)** | No switch, never dials | **Does not spawn a full local backend.** Starts the account-only backend (§6.4) and connects only through the relay | The switch, as in §4 |
| **igni-core full backend (offline, from the verified pack)** | — | The **`RunGate` closes** with a person-facing reason: "Your organisation runs igni for you in the cloud." This also stops a hand-started `igni` CLI. Sign-in and status still work | — |

**`cloud` mode on the desktop is a policy, not a sandbox.** A person can
always install an old igni-core that ignores the field. The run gate
makes the supported paths honour it. The guarantee that matters for
`local` mode (no cloud compute) is server-side and does not depend on the
client at all.

### 6.4 `cloud` mode's local process: an account-only backend (igni-core)

A desktop in `cloud` mode still needs four things:

- sign-in (the browser hand-off and `credentials.json`);
- server discovery;
- the group pack sync, which is how it learns the mode;
- `status_update` for the sign-in wall.

All four live in igni-core today. The decision is a new startup mode:

```
python -m igni_core.backend --account-only --ws-port 0
```

- It serves only the sign-in, discovery, plan and status RPCs.
- It runs the pack sync.
- It starts **no** agent, tools, MCP servers, extensions, watchers or
  event pull.
- Its `RunGate` is permanently closed. `user_message` gets the same
  refusal as §6.3.
- Its standing is the window's, and nothing else is reachable.

The account backend's socket is never what the chat windows point at.
The sign-in wall and the account menu reach it through the shell. In
phase 3, a window in `cloud` mode connects its chat to the relay and its
account calls to the account backend through a Tauri command
(`account_rpc(method, args)`), which forwards only the allow-listed
methods.

**Rejected: re-implementing sign-in and the pack fetch in Rust.** It
would duplicate tested Python: the callback server, `credentials.json`'s
format, JWKS caching and `min_version` state. Two implementations of
anti-rollback are worse than one. → Q7 offers the cheaper and stricter
variant if the owner wants no local Python at all in `cloud` mode.

### 6.5 Offline behaviour

| Situation | Behaviour |
|---|---|
| Never signed in, no pack | As today: a full local backend and the sign-in wall. Once signed in, the pack decides |
| Offline with a verified cached pack | The pack's mode holds. It has no `exp`, and anti-rollback stops an older pack from being replayed |
| `cloud` mode and the gateway unreachable | `CloudUnreachable`. **No fallback to local** (Q8), because the admin chose cloud, often for data-residency reasons |
| `local_and_cloud` and the gateway unreachable | `CloudUnreachable`, with **Use this computer** |
| The pack fails to verify (wrong pin, tampered) | The shell treats it as no group layer, as today, so the mode is `local_and_cloud`. igni-core's `group_policy.required` still applies where an admin set it |

### 6.6 The mode changes while the app runs

The shell learns the new mode three ways:

- the backend's pack sync rewrites `pack.jws` (every 5 minutes), and the
  shell watches the file;
- `/v1/me/cloud` polling;
- an immediate 1008 kick from the gateway.

| Change | What happens |
|---|---|
| any → `local`, while in the cloud | The gateway kicks (1008 `group_local_only`). The shell shows "Your organisation now runs igni on this computer", re-points the windows to local and hides the switch. Unsent drafts stay in the composer |
| `local_and_cloud` → `cloud`, while local | A banner: "Your organisation moved igni to the cloud. Switching when the current run ends." The shell waits until no local run is in flight (at most 10 minutes, then switches anyway with a notice), then switches, stops the owned full backend and starts the account-only one. Local approvals still waiting are reported first ("1 approval on this computer will be dropped") |
| `cloud` → `local_and_cloud` / `local` | The shell spawns a full local backend. `local_and_cloud` stays on the cloud with the switch shown. `local` re-points to local |
| A group change that only alters `vm_profile` | Nothing on the desktop. The gateway handles profile changes |

---

## 7. Who pulls `/v1/me/events` (D6)

**Today's race (§1.3–1.4).**

- Both sides lease from one queue, so the first to pull wins.
- A side without the extension acknowledges the event as `not_handled`,
  and the other side never sees it.
- Lost acknowledgements deliver the event twice, because the dedupe
  tables are separate.

**Rule: igni server holds one owner per person and serves only the owner.**

- **Storage.** A small table `person_event_owner(org_id, user_id, owner,
  updated_at)`. It is kept apart from `org_group_members` so that moving a
  person between groups does not reset it.
- **The effective owner.**
  - `local` mode → local;
  - `cloud` mode → cloud;
  - `local_and_cloud` → the stored value, `local` until the person first
    switches (Q9).
- **The desktop's pull** (`POST /v1/me/events/pull` with a session token)
  answers `409 {"error":"events_owned_elsewhere","owner":"cloud"}` when the
  owner is cloud. `ack` and attachment fetches for items already leased
  stay allowed: acknowledgements are idempotent by id, so an in-flight
  delivery finishes cleanly.
- **The gateway's platform pull** for that person returns `[]` when the
  owner is local. `/v1/platform/wakes` stops listing the person for
  pending events, so the VM is not woken for events it will not get.
- **On a change of owner, leases are left alone.** Items the old side
  leased and did not acknowledge come back after the 2-minute lease, to the
  new owner. At-least-once delivery is unchanged.
- **The switch sets the owner** (`PUT /v1/me/cloud/events-owner`) before it
  re-points the windows.

**igni-core change (`extensions/event_pull.py`).** A `409` with
`events_owned_elsewhere` is not an error. The pull idles and asks again
after `INTERVAL_SECONDS` (60 s), logging once at info. It needs no new
RPC and no shell call: the server is the single arbiter, so the two sides
never have to coordinate.

**Rejected alternatives:**

- **The shell tells the local backend to stop pulling.** It does not cover
  the gateway, which pulls for a sleeping VM. It does not survive the app
  being closed. And it needs a new RPC.
- **The desktop pulls only while it is open, and the cloud the rest of the
  time.** Events would follow whether the app happened to be open, not the
  person's choice, so they would land in a different place depending on
  timing.
- **A longer or per-side lease.** It still lets an event land on the side
  without the extension.

---

## 8. Error states (D7)

| Condition | Detected by | Shell state | Recovery |
|---|---|---|---|
| VM asleep or waking | The upgrade succeeded but no `welcome` arrived yet | `Waking` | Automatic. After 20 s the gateway closes with 1011, and the shell moves to `CloudUnreachable` with **Retry** |
| VM needs sign-in | The `vm_credential` push with `needs_sign_in` (replayed on attach) | `CloudNeedsSignIn` | **Sign in** → `cloud_sign_in()` (Tauri) → the shell calls `POST {gateway}/v1/sign-in` with the session → `credential.state`. When igni pushes `ready`, the banner clears |
| VM credential unreachable | `vm_credential` `unreachable` | `CloudReady` with a warning chip | Informational: the VM cannot reach igni server. Runs that need the server fail with their own errors |
| `needs_reissue` | Never seen by a person (an app-only state) | — | Logged. Nobody signs in as an app on the desktop (decided 2026-11-12) |
| Desktop session expired or revoked | Dial → 401 | `NeedsDesktopSignIn` | The existing sign-in wall. The relay re-dials when `credentials.json` changes |
| Gateway unreachable | DNS, TCP or TLS failure, or 503 (JWKS) | `CloudUnreachable` | Backoff from 1 s to 30 s, **Use this computer** where allowed |
| Gateway busy | 1013 | `CloudUnreachable("busy")` | The same backoff |
| Could not start | 1011 | `CloudUnreachable("could not start")` | **Retry**. The reason text is shown |
| Not an active producer, licence refusal | 1008 with a reason | `Refused(reason)` | Back to local if allowed. `/v1/me/cloud` re-checked |
| Group moved to `local` | 1008 `group_local_only`, the pack, `/v1/me/cloud` | → `Local` | §6.6 |
| Session moved between nodes | A second `welcome` on the same socket | Stays `CloudReady` | Re-bind and hydrate (§4.3) |
| TLS failure (bad certificate) | rustls | `CloudUnreachable("certificate")` | **No bypass in a release build.** A debug-only `IGNI_DEV_GATEWAY_INSECURE`, following `IGNI_DEV_NO_WS_TOKEN` |

**How the relay closes the window's socket.**

- **Transient upstream failures** close it with **1012 (service restart)**.
  The web client reconnects with its normal backoff, and the relay
  re-dials.
- **Final states** close it with **1001** after the shell has already
  re-pointed or flagged the window.
- **The relay never forwards 1008 to the window.** The web client reads
  1008 as "replaced", and the meaning would be lost.

---

## 9. Security (D9)

1. **The token in the webview: none.**
   - The session token is read by Rust from `credentials.json`. It is
     already there at 0600, and the shell already reads it for `sub`.
   - It is sent only in the upstream `Authorization` header, and is never
     logged. The relay's log lines name the gateway host and close codes
     only.
   - No Tauri command returns it. A test asserts that no command result
     and no window URL contains it.
2. **Where the token may go.**
   - igni-core's rule is that "igni server's token goes only to the origin
     of `api_url`" (`SECURITY.md`). The relay extends that trust to
     exactly one more origin: the `gateway_url` that `api_url` itself
     returned over verified TLS.
   - It must be `https`. A plain `http` gateway is refused outside a debug
     build (Q10 covers the lab's plain-HTTP setups).
   - A managed setting `fleet.gateway_url` may **pin** the gateway, and a
     discovered URL that differs is then refused. This is for air-gapped
     or regulated deployments.
3. **The relay's own door.**
   - It accepts connections only on `127.0.0.1`, with a webview or
     loopback `Origin` and the relay's per-launch token, compared in
     constant time. This is the same model as the backend's handshake
     (`transport/ws_auth.py`).
   - Without the token, a page in the person's browser cannot use the
     relay to reach their cloud igni.
4. **Origin checks at the gateway (igni__cloudvm).**
   - New config: `public.allowed_origins` (a list, empty by default).
   - A request **with** an `Origin` header must match the list, or it gets
     403.
   - A request **without** one comes from a program (igni-client, the
     relay) and is allowed.
   - With bearer-only auth there is no ambient credential, so cross-site
     WebSocket hijacking is not possible today. The check closes off a page
     that was handed a token, and gives the future hosted web UI an
     explicit allow-list.
5. **Token expiry at the gateway** (Q3). Today an expired token's socket
   lives on. The recommendation is that the gateway closes at `exp` (plus
   leeway) with 1008 `token_expired`. For the desktop this is harmless:
   the relay re-dials with the current token.
6. **The group pack** keeps its existing protections: signature, pins,
   `iss`/`sub`/`org` binding and anti-rollback.
   - A local user who can write `~/.igni` cannot forge `mode`.
   - They can delete the pack, which means `local_and_cloud` on the shell.
     That is acceptable, because the server enforces `local` (§6.3).
7. **Extension iframes** are not shown in cloud mode (phase 1), so no
   third-party page shares a window with a cloud connection.
8. **The session token reaching the VM** through `vm_sign_in` is existing
   behaviour (`CLOUD.md`). This design adds nothing to it.

---

## 10. Contracts and SDK changes (D8)

| Repo | Contract change |
|---|---|
| igni-sdk `contracts/` | **New `cloud-discovery.schema.json`**: the `/v1/me/cloud` response and the `PUT` body. It is the single source: igni-server has a test that its OpenAPI model matches it, igni-client validates against it, and the shell has a test (fixture copy, checked by `scripts/sync_contracts.py --check`). This is the "shared discovery" the TODO assigns to igni-sdk, as a contract rather than a TS library, because the desktop's discovery client is Rust |
| igni-server OpenAPI | `GET /v1/me/cloud`, `PUT /v1/me/cloud/events-owner`, `mode` on group read/patch, `409 events_owned_elsewhere` on `/v1/me/events/pull`, the platform feed row's `inactive_reason` |
| igni-server pack | Top-level `mode` appended to `OrgGroupPack`. The field order stays documented |
| igni__cloudvm | `GET /v1/me` gains `vm{state, signed_in}`. `public.allowed_origins`. 1008 reasons become stable tokens (`group_local_only`, `inactive`, `licence`, `token_expired`) followed by human text, documented in `docs/gateway.md` |
| igni-core | `GroupPolicyPack.mode` (optional, default `local_and_cloud`). `--account-only`. The cloud layer refuses native-dialog RPCs. Event pull treats 409 as idle. **No wire-schema or RPC-contract change is required** (`make contracts` re-run to prove it) |
| igni-client | `discover(server_url, auth) -> CloudIgni(gateway_url, mode, cloud, reason)`, which reads `igni_gateway` and `/v1/me/cloud`. `Fleet.from_server(server_url, auth=Person(...))`. A typed `GroupIsLocalOnly(NotAllowed)` for 1008 `group_local_only`. Existing `Fleet(url, auth=)` is unchanged |
| igni-app | New Tauri commands `backend_state`, `switch_backend`, `cloud_sign_in`, and in phase 3 `account_rpc`. The event `igni-backend-state`. The new localStorage key scheme |

---

## 11. The model contract (D10)

Nothing here changes what the model reads or writes. The client protocol
is relayed byte for byte. Sessions, prompts, tools, `<attached-files>`
and the other injected blocks are built by the igni that runs the
session, local or in the VM, exactly as today. Attachments in the cloud
use the same upload path and `@path` reference. Service events are
delivered by whichever side owns them, through the same inbox.

The new person-facing texts are not model-facing:

- the `RunGate` refusal in `cloud` mode;
- the banners;
- the 1008 reasons.

None of them reaches a prompt.

**The trap to avoid.** Do not "tell the agent it runs in the cloud" by
adding a system-context line such as "you are in a cloud VM, local files
are unavailable" or similar. That **would** be a model-facing change:
**QUESTION: MODEL-CONTRACT.** It is out of scope here. If phase 4 or 5
ever seems to need it, it goes to the owner first.

---

## 12. Test plan

The rules are those in the memory "tests must be exhaustive":

- each behaviour gets a test that fails on the old code;
- mutation-proven where it is cheap;
- and the full CI run locally before each push.

### 12.1 Per repo

**igni-server (pytest, real Postgres)**

- `/v1/me/cloud`:
  - Fleet not configured → `unavailable/fleet_not_configured`;
  - each mode;
  - an inactive member;
  - an app, space or VM token → 403;
  - ETag and 304.
- events-owner:
  - `PUT` per mode, with 409 on a forbidden owner;
  - the default by mode;
  - it survives a group move;
  - it is audited.
- Event pulls:
  - a desktop pull answers 409 when the owner is cloud;
  - ack and attachments still work for leased items;
  - the platform pull is `[]` when the owner is local;
  - `/wakes` drops the person;
  - the existing test `test_the_gateway_and_the_desktop_share_one_lease` is
    **replaced** by `test_only_the_owner_is_served`.
- Feed: a group set to `local` → its rows go `active:false,
  inactive_reason`. The trigger fires on `mode` (this test fails without
  the trigger change).
- The `vm-sign-in` grant and renewal refuse a local-only person.
- The pack carries `mode` after `theme`. The signature still verifies with
  igni-core's verifier (a cross-repo fixture). The ETag changes when the
  mode changes.
- The group PATCH writes `mode` and the audit detail.

**igni-server portal**

- Vitest for the card: three choices, the warning before a kicking
  change, MSW handlers.
- Playwright functional run against `tools/portal-e2e-stack.sh`.
- New visual baselines for the card, and an axe check.

**igni__cloudvm (cargo, `GATEWAY_TEST_DATABASE_URL`)**

- `/v1/me` `vm` for asleep, running and none, and the route never wakes
  the VM.
- `allowed_origins`:
  - no Origin → allowed;
  - a listed Origin → allowed;
  - any other Origin → 403;
  - the default (empty list) refuses every Origin.
- 1008 `group_local_only` on wake and on kick (a `cluster.rs` case).
- `POST /v1/sign-in` → 403 for an inactive person.
- Token-expiry close, if Q3 is accepted.

**igni-core (pytest)**

- `GroupPolicyPack.mode`: absent → `local_and_cloud`; an unknown value →
  `local_and_cloud` with a warning.
- The `RunGate` closes under `cloud`, with the person-facing reason. Sign-in
  RPCs still work.
- `--account-only` serves its allow-list and refuses `user_message` and
  every agent, tool and extension RPC. It starts no event pull or MCP.
  Startup memory is measured and recorded.
- The event pull idles on 409 (no busy loop, one log line) and resumes
  when the owner comes back.
- The cloud layer refuses `pick_dir_native`.
- `make contracts` shows no diff.

**igni-sdk (pytest)**

- `discover()` against a fake igni server (FakeIgni grows
  `/.well-known/openid-configuration` with `igni_gateway` and
  `/v1/me/cloud`).
- `Fleet.from_server` with FakeGateway.
- `GroupIsLocalOnly` mapping.
- `cloud-discovery.schema.json` validated against recorded server
  responses.
- The contract sync check.

**igni-app (cargo, vitest, Playwright)**

Rust:

- The relay:
  - an upstream `Authorization` header and `igni.v1`;
  - refuses a foreign Origin, no token, or a wrong token;
  - reads `credentials.json` on every dial and closes all upstreams when it
    changes;
  - maps 1013, 1011, 1008 and 401;
  - never writes the token to logs (a log capture test);
  - refuses an `http` gateway in release.
- The state machine: a table test of every transition in §4.2.
- The mode from the verified pack: `local` hides the switch; `cloud`
  spawns `--account-only` and never the full backend; a tampered pack →
  `local_and_cloud`.
- The CSP contract test is **unchanged and still passes**.

Web (vitest):

- a second `welcome` re-binds;
- the key migration;
- the switch shows per `cloud`/`mode`;
- banners per state;
- cloud-hidden features are hidden;
- the waking overlay appears only after 800 ms.

Playwright (mock backend):

- a `cloud` fixture that emits the states through a test hook;
- new visual baselines for the switch, chip and banners
  (`scripts/baselines-linux.sh`).

**Desktop CI (igni-app `tauri-smoke`, xvfb).** igni-app has no
tauri-driver yet (`ci.yml` defers it). The CI proof is a Rust integration
test that:

1. runs the supervisor, the relay and a **stand-in gateway** (igni-sdk's
   FakeGateway, relaying to a real igni-core backend started by the test);
2. switches to cloud through the `switch_backend` command;
3. asserts a `welcome` and one turn through the relay with a fake model;
4. asserts the local backend's PID is still alive;
5. switches back.

The web half runs in Playwright with `ws=` pointed at the relay. A
tauri-driver job stays the recommended follow-up, and is not a blocker.

### 12.2 The live E2E that turns R691 green (the ember-dev KVM host)

**Stack.** Everything is built from the phase branches:

- igni-server;
- a real gateway and node agent (igni__cloudvm);
- a Firecracker guest with the branch's igni-core;
- a real model.

The desktop is the Linux build under xvfb on the host, or a macOS build
reaching the host's gateway through a tunnel. One script,
`r691_live.py`, writes its evidence under the campaign directory.

1. **Discovery.** Org, group G in `local_and_cloud`, person P signed in on
   the desktop. `/v1/me/cloud` is `available`. The switch shows.
2. **Switch and wake.** P's VM is asleep (checked with the admin route).
   Flip to Cloud: the "waking" overlay shows, then a `welcome`, then a
   real-model turn answers PONG. The local backend PID is unchanged. A
   local session is still listed after flipping back.
3. **Per-backend state.** The localStorage keys for local and cloud
   differ, and each side hydrates its own windows.
4. **Events owner.** A service posts an event. It is delivered in the
   cloud only (the VM's `delivered_events` has it, the desktop's SQLite
   does not). Flip to local: the next event is delivered locally only.
   No event lands on both sides.
5. **Needs sign-in.** Revoke P's VM credential (portal
   `/portal/vm-credentials`). The banner appears. **Sign in**, and the
   state goes `ready`, then PONG.
6. **Node move.** Drain P's node (two nodes, as R708 did). A second
   `welcome` arrives, the client re-binds, and the next turn answers PONG
   with no window reload.
7. **Gateway unreachable.** Stop the gateway unit: the banner shows and
   **Use this computer** works. Start it again: the app recovers.
8. **Mode to `local`.** The live cloud socket is kicked within 10 s, the
   app goes back to local and the switch hides. A new connect gets 1008
   `group_local_only`. igni-client raises `GroupIsLocalOnly`.
9. **Mode to `cloud`.** After the local run ends, the shell stops the full
   backend and starts `--account-only`, and the chat runs in the cloud.
   Restart the app offline (a dead proxy, as in "air-gap proof without
   disk"): it stays in `cloud`, shows unreachable, and spawns no full
   backend. A hand-started `igni` CLI run is refused by the `RunGate`.
10. **Security probes.**
    - No session token in the webview: devtools search of window globals,
      localStorage, URLs and the network panel.
    - The CSP is unchanged.
    - The relay refuses a foreign Origin.
    - The gateway refuses an unlisted browser Origin.

This run also closes **R321's live proof** (step 5, against a real
gateway). R691 goes green on steps 1–4 and 6–7 at the end of phase 2,
and is re-run in full after phase 3.

---

## 13. Phasing, merge order and estimates (D11)

One PR per repo per phase, merged in this order inside each phase:
**igni-server → igni__cloudvm → igni-core → igni-sdk → igni-app.**

- The server endpoint and contract come first.
- The gateway and igni-core accept them.
- The SDK carries the shared schema.
- The app consumes it all.

Each repo PR runs its full CI locally before its push. Estimates are
engineer-days, and include tests and docs.

### Phase 1: discovery and the switch, chat only (extensions hidden)

| Repo | PR | Days |
|---|---|---|
| igni-server | `/v1/me/cloud` (mode reported as `local_and_cloud` until phase 3), `person_event_owner` with the `PUT`, pull enforcement (409 / `[]` / wakes) | 2.0 |
| igni__cloudvm | `/v1/me` gains `vm{state, signed_in}` | 0.5 |
| igni-core | Event pull idles on 409. The cloud layer refuses native-dialog RPCs | 0.5 |
| igni-sdk | `cloud-discovery.schema.json`, `discover()`, `Fleet.from_server` | 1.0 |
| igni-app | Relay, discovery client, state machine, Tauri commands, switch and chip, key migration, second-`welcome` re-bind, waking overlay, cloud-hidden features, tests, baselines | 6.0 |
| **Total** | | **10.0** |

### Phase 2: VM wake, sign-in and status

| Repo | PR | Days |
|---|---|---|
| igni__cloudvm | `allowed_origins`, stable 1008 reasons, close at token expiry (if Q3 is accepted) | 1.0 |
| igni-app | `cloud_sign_in` and its banner, unreachable, busy and could-not-start states, the badge for local runs and approvals, TLS and proxy handling | 2.5 |
| igni-sdk | Docs: Fleet discovery and the sign-in story | 0.5 |
| Live E2E | Steps 1–7 on the ember-dev host. **R691 green**, R321's live proof | 1.5 |
| **Total** | | **5.5** |

### Phase 3: group mode in the portal, and its enforcement

| Repo | PR | Days |
|---|---|---|
| igni-server | `org_groups.mode` migration and trigger, PATCH and read model, pack `mode`, feed `inactive_reason`, grant refusal, `/v1/me/cloud` real mode, audit detail, portal card with vitest, Playwright and visual tests | 3.5 |
| igni__cloudvm | Feed `inactive_reason` → 1008 `group_local_only`, `/v1/sign-in` 403 | 0.5 |
| igni-core | `GroupPolicyPack.mode`, `RunGate` in `cloud` mode, `--account-only` | 2.5 |
| igni-sdk | `GroupIsLocalOnly`, contract sync | 0.5 |
| igni-app | The shell reads `mode` from the verified pack, `cloud` mode's no-spawn plus account backend plus `account_rpc`, live mode changes (§6.6) | 3.0 |
| Live E2E | Steps 8–10, then a full re-run | 1.5 |
| **Total** | | **11.5** |

### Phase 4: extensions over the gateway (outline; its own design doc)

Phase 4 needs three things:

- **A page mark the node agent attests**, so that the cloud igni can let
  one connection speak for an extension (`SECURITY.md` L555).
- **Hosting extension pages from somewhere the desktop can frame.** Today
  the CSP `frame-src` allows only loopback. The likely shape is that the
  relay also proxies page origins.
- **`extension_service_token`** in the cloud.

Rough size: 2–3 weeks across igni-core, igni__cloudvm and igni-app. It
is not estimated further here.

### Phase 5: both side by side (maybe)

A window per side, so `local_and_cloud` people can keep a local chat
next to a cloud one. Most of the groundwork comes earlier: the per-window
`ws=`, the per-backend keys and the per-window relay connections. The
missing piece is a per-window choice in place of an app-wide one, plus
the events-owner question, because ownership stays per person. Estimate
on request.

### Totals for phases 1–3

| Repo | P1 | P2 | P3 | Sum |
|---|---|---|---|---|
| igni-server | 2.0 | — | 3.5 | 5.5 |
| igni__cloudvm | 0.5 | 1.0 | 0.5 | 2.0 |
| igni-core | 0.5 | — | 2.5 | 3.0 |
| igni-sdk | 1.0 | 0.5 | 0.5 | 2.0 |
| igni-app | 6.0 | 2.5 | 3.0 | 11.5 |
| Live E2E | — | 1.5 | 1.5 | 3.0 |
| **Sum** | **10.0** | **5.5** | **11.5** | **27.0** |

Add about one day per phase for the merge chain and CI round trips:
**about 30 days end to end** for phases 1–3.

---

## 14. Open questions for the owner

Each question carries a recommendation. Approving the doc as written
means accepting every recommendation.

| # | Question | Recommendation |
|---|---|---|
| Q1 | Discovery is split: igni server answers whether there is a cloud igni (and the gateway URL, mode and events owner); the gateway's `/v1/me` answers the live VM state. Is that acceptable, against "igni server tells the app the VM's state"? | **Yes, split.** The server has no VM state, and fetching it would couple the server to the gateway's admin API |
| Q2 | Token hand-off: the shell relays, with the token kept out of the webview, instead of the webview sending `igni.bearer.<token>` as the TODO says | **Relay.** The gateway's token is the 30-day session token, and the CSP stays loopback-only |
| Q3 | Should the gateway close sockets at token `exp`? And should a future hosted web UI get short-lived person tokens? | **Yes to both, later.** Closing at `exp` lands in phase 2 (cheap, harmless for the relay). Short tokens are needed only when a browser UI exists |
| Q4 | When a group switches to `local`, are its people's VMs kept (inactive) or erased? | **Kept**, under the profile's idle-erasure rule. Flipping a group back must not lose work. An admin can reset a VM explicitly |
| Q5 | Older desktops ignore `mode`. Do we force an update for `cloud` groups? | **No force.** `local` is enforced server-side regardless. For `cloud`, document "requires desktop ≥ X". Optionally the portal shows members on older versions later |
| Q6 | Should the app refuse a cloud igni with a different wire major? That needs a version field in `welcome`, a client-protocol change but not a model-facing one | **Not in phase 1.** Rely on additive compatibility. Add `welcome.wire_version` only if a breaking protocol change is ever planned |
| Q7 | In `cloud` mode, may the desktop still run a local Python process (the account-only backend), or must sign-in move into the Rust shell? | **The account-only backend.** It reuses the tested sign-in and pack code and runs no agent. Moving sign-in to Rust is about 3 extra days in the shell and duplicates anti-rollback |
| Q8 | In `cloud` mode with the gateway unreachable, fall back to local? | **No.** The admin chose cloud. Show unreachable and wait |
| Q9 | Who owns events in `local_and_cloud` mode before the person first switches? | **Local** (no change for people who never use the cloud). People who use Fleet only through igni-client can set the owner with `PUT events-owner` (a one-line SDK call `set_events_owner("cloud")`) |
| Q10 | The lab gateway on the KVM host is plain HTTP. Allow `http` gateways? | **Only in debug builds** (`IGNI_DEV_GATEWAY_INSECURE`). The live E2E uses TLS on the host's gateway, or the debug build if TLS is not set up yet |
| Q11 | Is igni-core the right home for this doc? | **Yes for the doc** (TODO, CLOUD.md and SECURITY.md are here). Each PR links to it. The gateway parts get a short section in igni__cloudvm's `docs/gateway.md` once they are built |

**Out of scope.** The TODO's companion item, "Seeing what an app does,
without signing in as the app", is meant to become a section of this
design. It is left out, because the decision of 2026-11-12 settles the
part that touches the switch: **the switch only ever connects as the
person**, never as an app. That item's allow-list and SDK command can
follow as their own section once this one is approved.
