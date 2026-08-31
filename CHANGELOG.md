# Changelog

## Unreleased

- Normalize WeChat article-image aliases from `mmecoa.qpic.cn` or `mmbiz.qpic.cn` to canonical HTTPS `mmbiz.qpic.cn` URLs before validation, storage, and draft submission.
- Keep the 16 most recently used client tokens per user so pairing Codex, Trae, or another client no longer revokes existing clients; password changes still revoke all tokens.

## 4.0.0 - 2026-08-30

- Replace all static client API keys with a unified token issued from a 60-second, single-use pairing code.
- Store only token hashes, revoke the previous token on re-pairing, and revoke client tokens on password changes.
- Remove legacy key display, environment configuration, rotation scripts, and the legacy service-credentials table.
- Use the unified token for image upload, temporary images, and local or remote WeChat draft CRUD.
- Add AI endpoints to list, inspect, update, and delete WeChat drafts through the official draft APIs.
- Add direct WeChat draft-box listing and media-id CRUD for drafts without local task records, while merging partial updates against the latest remote article.
- Hard-delete local draft jobs after remote deletion so deleted records no longer accumulate in the console.
- Treat already-missing permanent materials as remotely absent and clean their local records; WeChat article-image URLs still have no deletion API.

## 3.2.2 - 2026-08-20

- Add a product-focused console visual and a creator-value visual to the GitHub homepage.
- Move the real overview screenshot into the console gallery alongside the API screenshot.
- Include the new README visuals in verified server release archives.
- Change the localhost listener and reverse-proxy target to `127.0.0.1:8791` while keeping the container port at `8000`.

## 3.2.1 - 2026-08-20

- Add real console screenshots and a shared end-to-end project workflow to the GitHub homepage.
- Present the console server and article-design skill as one workflow with bidirectional documentation links.
- Include README visuals in verified server release archives.

## 3.2.0 - 2026-08-18

- Add first-run console setup with encrypted, automatically generated API keys.
- Store administrator passwords as Argon2 hashes and support immediate password rotation with full session revocation.
- Add self-service user registration with administrator and regular-user roles.
- Add per-user management and switching for multiple subscription or service accounts.
- Isolate account credentials, assets, temporary images, and drafts between users and official accounts.
- Migrate legacy single-account data to schema v3 while revoking pre-migration sessions.
- Let administrators review all users' drafts with owner and official-account labels while keeping other users' drafts read-only.
- Protect first-run setup with a one-time code and bind the Docker port to localhost by default.
- Recover schema-v1 account, asset, temporary-image, and draft ownership after administrator creation.
- Add registration throttling, transaction-safe user limits, account limits, and per-user temporary-storage quotas.
- Add session-authenticated draft creation, paginated draft listings, and cross-account deletion for administrators' own drafts.
- Clear user-specific browser state before switching sessions.
- Open the stable WeChat Official Account entry point in a new tab without relying on an expiring web token.

## 3.1.1 - 2026-08-18

- Add an admin-only Skill client configuration panel with masked keys, one-click copying, and no-store responses.

## 3.1.0 - 2026-08-17

- Connect the article designer Skill to the image and draft APIs.
- Prevent automatic draft retries when the WeChat result is ambiguous.
- Add login throttling and bounded-memory image uploads.
- Add schema versioning, diagnostics, reproducible release packaging, and CI.
- Split server deployment and Codex Skill into separately verified release archives.
- Move the Codex Skill to its own GitHub repository and keep this repository server-only.
- Add MIT licensing and open-source contributor documentation.

## 3.0.0 - 2026-08-17

- Turn the image uploader into a WeChat management console.
- Add encrypted account credentials, image APIs, draft jobs, and the console UI.
