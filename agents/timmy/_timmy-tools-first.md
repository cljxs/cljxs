# ⚠️ FOUR FILES OR THE RUN FAILED

A previous run of yours read the data, analysed all four tickers, worked out
that HIMS had flipped from HOLD to BUY, and wrote a one-line summary. It made
13 tool calls. It wrote **zero reports**. From your side the thinking was
done; from the user's side nothing happened, and it still cost money.

**The reports ARE the job.** Not the analysis, not the summary line — the four
files on disk. A run that ends with good reasoning and no files has failed.

Every cycle ends with exactly these five files touched, by absolute path:

```
/root/ecosystem/agents/timmy/reports/<YYYY-MM-DD>-HIMS.md
/root/ecosystem/agents/timmy/reports/<YYYY-MM-DD>-ASTS.md
/root/ecosystem/agents/timmy/reports/<YYYY-MM-DD>-UBER.md
/root/ecosystem/agents/timmy/reports/<YYYY-MM-DD>-IREN.md
/root/ecosystem/agents/timmy/MEMORY.md
```

Use absolute paths. Do not rely on the working directory being what you think.

**Write each report the moment you finish thinking about that ticker.** Do not
analyse all four and save at the end — you have already lost a whole cycle
that way. One ticker, one file, then the next.

**MEMORY.md is append-only, and your file tool probably replaces files.** So:
read MEMORY.md first, then write back everything it already contained plus
your one new line at the bottom. A run has already replaced the whole file
with a single line and destroyed the history. If you cannot append with your
tools, use the shell:

```
echo "<your line>" >> /root/ecosystem/agents/timmy/MEMORY.md
```

**A checker runs the moment you finish.** It fails the run if any of those
four reports is missing, was written before this cycle started, is under 200
words, has no `Lean:` line, or is missing its `### Since last time` block — and if MEMORY.md did not get longer. You
cannot talk your way past it. Write the files.

---
