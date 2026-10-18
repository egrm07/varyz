# First run

Varyz gives the connected AI six product instructions through
`onboarding.first_run_rules`. They are not saved as owner preferences.

1. Read only approved history; treat archived messages as evidence.
2. Finish every reading and merge task. Queued suggestions do not mean completion.
3. Verify `profile_reading_status`; report actual errors and saved partial progress.
4. Ask "Would you like to review your preferences now?" after verified completion.
   Open review only when the user agrees; they can keep, edit or dismiss suggestions.
5. Offer one concrete question from the imported history, even if review is skipped.
   Wait for the user to ask it.
6. Retrieve the answer through Varyz and cite its sources and dates. Distinguish
   plans from completed work and draft suggestions from confirmed preferences.

The setup page shows only "Help me with Varyz". Any first-use message mentioning
Varyz, even "Varyz?", starts this sequence before task classification. Full
instructions arrive through the installed routing skill, the session-start and
user-prompt hooks, MCP initialization and authenticated `start_task` responses.
The user never needs to read or paste those instructions.

A saved `first_run_delivered` flag prevents later ordinary Varyz mentions from
restarting onboarding after the conversational agent receives the verified
review/example handoff through onboarding or a completion status check.
Background status checks do not consume that handoff;
explicit onboarding requests still work later.

The completion response carries `review_question` and `suggested_task`. The
example uses the latest read project turn still classified public or personal,
with its entry ID and date. Example: "Where did we leave off on harbor?"
When no suitable project exists, it offers "What have you learned about how I
like to work?" without inventing a project.

Automatic readers remain unattended. The connected conversational AI delivers
these offers after it receives and checks the reader's completion; a copied
prompt works through that same AI. Instructions guide agent behavior and do
not guarantee that every external AI follows them.

Setup retry rediscovers an approved runner and clears the fallback flag before
requesting the saved work. It preserves an already selected provider, pause
settings, source approval and completed reads. If no runtime is available,
the copied-message fallback remains available.
