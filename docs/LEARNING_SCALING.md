# Large chat histories

**2026-10-20:** Preference learning selects the newest 4,000 eligible owner messages.
The setup offer states how many older messages are left out. The archive retains
those messages, and existing learned notes keep their original evidence. Local,
subscription and agent readers use the same window; sensitivity and redaction
rules still apply. The limit bounds messages, not the total number of model tasks:
long messages continue to be read in overlapping excerpts.

Parsed turns are cached per archive and privacy tier. Changes to message identity,
content hash, sensitivity, conversation, date or assistant context invalidate the
cache. Evidence is revalidated against the complete eligible archive, including
messages outside the recent window.

A background reader with no reading-state activity for 10 minutes stops instead
of consuming its full 90-minute runtime limit. `reader_stall_minutes` in
`[workflows]` controls that interval. Completed work remains saved. The supervisor
can try another available reader and reports failure after repeated runs without
progress; it does not claim learning finished.

The setup page suppresses its countdown after three minutes with zero messages
read. It shows elapsed time instead. A progress refresh more than one minute old
is labelled stale and loses its countdown. Before the first successful progress
measurement, setup says it is starting the reader rather than displaying a
completed message count.

Setup no longer offers the general Documents directory. The current repository
checkout can still be offered as **Project files**, including when that checkout
is inside Documents. Conventional code folders are still searched for repository
history. Existing explicitly approved connector configurations are preserved.

## Verification

Two deterministic synthetic archives each contained 28,000 eligible messages.
Each selected 4,000, retained 24,000 older unread messages and completed the selected
window in 260 tool batches. Median task/accept/progress overhead fell from
1.4868 to 0.3335 seconds in the first archive, and 1.9260 to 0.3314 seconds in the
second. The baseline measured eight batches without caching or a recent window;
the candidate completed its entire selected window. These are tool-processing
measurements on Windows, excluding AI generation and preference-quality evaluation.
They do not establish complete learning time or reproduce a physical Mac failure.

Regression tests cover source discovery, recent-window completion, retained old
evidence, cache invalidation, stalled readers, resumed activity and unknown/stale
progress. Browser checks cover the offer, starting, zero-progress and stale states
at desktop and mobile widths, with no page errors or horizontal overflow.
