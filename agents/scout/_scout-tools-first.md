# ⚠️ THIS IS A FILE-WRITING JOB, NOT A CONVERSATION

Your first run generated three perfectly good ideas and **printed them in the
reply**. It made **zero tool calls**. Nothing was written. From your side the
work was done; from the user's side nothing happened, and it still cost money.

**Listing ideas in your reply is NOT doing the job.** The job is done when the
ideas are in `state/ideas.json` **on disk**.

Every run, you must actually use your file tools:

1. **READ** `state/ideas.json` — you need the existing ideas and the highest id
2. **READ** `../emily/state/lessons.md` — approvals and rejections
3. **WRITE** `state/ideas.json` — existing entries kept, yours appended
4. **WRITE** `reports/<today>.md`
5. **APPEND** one line to `MEMORY.md`

Read first, then write. Never overwrite `ideas.json` with only your new ideas —
you would delete everything already in it, including ideas the user has not
reviewed yet.

**If your reply contains ideas but you made no tool calls, the run failed.**
The service now checks whether `ideas.json` actually changed and reports a
failure if it did not, so a run like the first one will no longer be logged as
a success.

---

