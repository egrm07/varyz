# Local Varyz setup

**Find relevant earlier decisions in sources you choose, and use them in your current AI task. Local Varyz is free.**

The prepared local-companion ZIP contains the Python package, corresponding source, consent-based installers and local Codex/Claude Code plugins. It does not require access to the private Git repository. Public downloads are not live yet: use only a release supplied by the publisher; no public package or download URL is promised.

The same release installer supports fresh installations and upgrades of existing **uv-managed Varyz tools**. Before upgrading, close your AI agent sessions and stop the Varyz daemon/background sync. The installer refuses a busy installation or an unavailable process-safety check. Source checkouts, other package managers, prerelease versions and unsupported custom uv receipt options require their own reviewed update path.

1. Download the published ZIP once available. Compare its SHA-256 with the independently published release checksum, then extract it. A checksum inside the ZIP checks contents but does not establish publisher identity.
2. Windows: open `Install Varyz.cmd`. macOS: open `Install Varyz.command`. These open the setup consent page. Linux: run `sh install.sh`. An agent can preview with `install.ps1 -DryRun` or `sh install.sh --dry-run`. The installer uses the included wheel. Network access is still needed for runtime dependencies and selected local models. It may install uv if absent. No Cloud account is needed.
3. Review the setup page. Choose the folders/sources and AI agent connections yourself. Installation can add runtime dependencies, local models and a background service. Don't use automatic approval options to bypass these choices.
4. Wait for the selected sources to finish their first index. Ask your local AI agent to find one earlier decision relevant to your current task. It should return a real source reference and explain how it helps. No result means no relevant evidence was found; it is not proof setup failed.
5. Local retrieval may send excerpts to your connected AI provider as part of the task. It does not upload your archive to Varyz Cloud. Cloud syncing is a separate request and separately approved selection of confirmed preferences.

The bundled `plugins/` folder is the local marketplace root. Its `codex` and `claude-code` packages are for local clients, not hosted ChatGPT. After a user requests plugin installation, the agent can use its setup skill to check the installed connection and preserve existing approved credentials. Do not install on a ChatGPT execution sandbox and claim it connects to the user's computer.

For an existing Cloud subscription, ask your local agent to activate Varyz Cloud. It will check sign-in, confirm active access, ask which project scopes to sync, preview counts, request upload approval, and verify an existing rule through hosted MCP. Phone availability depends on an actually supported and published connection; it is not established by local installation.

To stop Cloud syncing, request disconnect. This leaves local preferences available and existing Cloud records in the account. Removing local sources, stopping the background service and uninstalling Varyz are separate operations documented in the main README.

## Existing installation upgrades

The release installer validates its included wheel checksum and version, retains the old uv environment, command launchers, configuration and a consistent SQLite backup, then installs the wheel from a persistent local release cache. It verifies imports, configuration compatibility and database migrations on a copy before replacing the live archive. A failure restores the old environment and archive, or reports the retained recovery folder if automatic restoration cannot finish. Configuration, source choices, Cloud sync settings and OS credential-store grants are kept; upgrade does not rerun onboarding or connect additional agents.

Backups live under the configured data folder in `autosync/releases/<upgrade-id>/`. They are private and can be large: the old environment and archive are retained, along with failed artifacts where needed. Backups are not automatically deleted. A repeated verified release is skipped. Downgrades and migration mismatches are refused. Future updates use the next verified release installer; a downloaded wheel is never treated as a package-index subscription/update source.

After an interruption, use the reported folder with `install.ps1 -Recover <folder>` on Windows or `sh install.sh --recover <folder>` on macOS/Linux, using the same configuration (set `VARYZ_CONFIG` if needed). Recovery works even if the current tool receipt or live archive is missing. It refuses to overwrite an archive or configuration changed after interruption; preserve those files and review recovery manually. A completed upgrade is not silently undone by this recovery command.

Restart your agents and daemon after success. Real full-product upgrades across supported operating systems remain release checks; isolated uv tests verify installation, rollback and migration behavior without replacing the developer's live installation.
