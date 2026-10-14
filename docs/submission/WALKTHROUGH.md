# Reviewer walkthrough

> Updated 2026-10-06. Actual native ChatGPT web MCP scenarios are recorded in
> `reports/walkthrough_2026-10-06/final/varyz-native-mcp-review.mp4` (4m14s).
> [Public reviewer video](https://cloud.varyzai.com/review/varyz-native-20261006/varyz-native-mcp-review.mp4)
> plays anonymously. This tests the hosted MCP connection; native desktop skill
> bundle and Claude verification remain open.

The edited video uses authentic native browser screens, offline narration and
captions. It is not continuous screen capture. All five positive and three
negative scenarios were exercised. The SQLite answer initially omitted citations;
an explicit follow-up produced the source URL, date and authorship. The generic
upgrade prompt initially meant ChatGPT; a Varyz-specific follow-up established
the plugin's billing boundary. Both limitations are narrated, not hidden.

The explicit rule write persisted in the synthetic Cloud account (3 → 4
preferences). Disconnect required refreshed sign-in; a fresh dashboard then
showed zero connections. A newly invoked native fetch failed after revocation
without reconstructing previous text. The host reports a generic tool error;
its interface does not expose raw MCP arguments, payloads or HTTP status.

Public bytes match local SHA-256
`538be36024c46d1e4c92ab9f4c16137972435ca252191082da92e68f5dcd1804`;
anonymous HTTP 200, byte-range HTTP 206 and browser playback passed.
Decoded frames were visually reviewed. Audio decoded without errors and peaks
at −3.1 dB; by-ear listening was unavailable. Credentials and owner conversations
are excluded. The earlier static dashboard tour remains an explicitly incomplete
draft and is not the submitted recording URL.

Record actual native ChatGPT and Claude behavior using the dedicated synthetic
account. Keep the browser sidebar closed and hide email/password fields, OAuth
codes, tokens, unrelated chats and personal account settings. Do not substitute
mockups, CLI output or screenshots for successful native tool execution.

1. Show the Varyz listing, skills and connector URL. Explain existing account access.
2. Start OAuth sign-in; stop recording while entering credentials. Resume on the
   consent screen and show preference access plus read-only approved history.
3. Connect successfully and show the actual tools available in the host.
4. Ask the exact known preference and show its returned confirmed answer.
5. Ask why demo chose SQLite; show `search`/`fetch` and source/date citations.
6. Ask where demo was left and what changed. Show `timeline`/`compare`, the completed
   database decision, pending timeline review and both retained versions.
7. Explicitly request remembering the demo short-reply rule; show the successful write.
8. Show unsupported local-file, checkout and other-account requests handled correctly.
9. Revoke the test connection from the Varyz account and show that further access fails.

Review the actual frames and audio before delivery. Host the finished video at an
accessible reviewer URL; verify anonymous playback from outside the publisher account.
Set `demo_recording_url` in canonical `docs/submission/review-assets.json`; the
generator copies it to `extensions.com.openai.review` in both manifests. Rebuild
and checksum the exact final package. Never
use an example/placeholder URL or claim this script is a completed walkthrough.
