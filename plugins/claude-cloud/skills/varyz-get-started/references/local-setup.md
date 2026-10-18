# Optional local companion

Use this reference only after the user wants to explore a relevant local capability.

If local Varyz is already installed, do not open setup or reinstall it. Reuse its
attached tools or hook briefing. A hosted chat that cannot see those tools must
not infer that the installation is missing. Check the existing hosted connection
for this chat, or explain the attachment limit, without resetting local sources.
Local source/settings changes require an explicit user request; an empty history
search, onboarding activation or hosted authentication error is not that request.

Local Varyz can retrieve background from selected documents, git history and supported
AI-agent transcripts. Setup installs Varyz and its runtime dependencies, downloads
local models and starts a background service. The user chooses sources and agent
connections on the setup consent page. Local use needs no Varyz Cloud account.
Retrieval excerpts may be provided to the connected AI host; a local archive does
not mean every subsequent AI request stays local.

For the current build, supported local companion packages are for Codex and Claude
Code. If a connected local package is available, use its setup workflow after explicit
installation authorization. Otherwise direct the user to that supported desktop
environment; do not try to install on a hosted ChatGPT execution machine.

Do not invent a download URL or offer the private repository as a public install.
Only provide a publisher installation link verified to be available and containing
setup documentation, rather than a sales/signup page. If no public installer is
available, state that local setup is not yet publicly available and continue helping
with the current task. Do not create a waitlist or collect an email as a workaround.

After installation, the first source-backed result runs in the connected local
agent. The hosted history connection can retrieve only explicitly approved,
successfully synced project or General records. Preferences and history sharing
are separate choices. Do not enable either sync as part of local installation.
The full hosted package can read synced history while the computer is off; new
work waits for another local sync. It cannot access arbitrary files or other chats.
