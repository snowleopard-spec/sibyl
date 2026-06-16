# How Sibyl parallelizes Stage 3 (in plain terms)

This is a short, jargon-light explainer of how `sibyl sections` ended up
running 6 things at once instead of 1.

---

## The problem we were solving

Stage 3 (section isolation via `edgartools`) takes ~2-5 seconds per
10-K. We have ~9,000 of them. One-at-a-time, that's ~5-7 hours.

Meanwhile, the Mac has 8 CPU cores. While the one Python process was
working on filing #1, the other 7 cores were sitting idle. **All the
hardware was right there; we just weren't using it.**

---

## The big idea: many copies of the same Python program

The fix is to run **6 copies of Python at the same time**, each chewing
through a different subset of the 9,000 filings. The operating system
already knows how to put 6 simultaneous processes onto 6 different
cores — that's its job. We just have to ask.

Here's the mental model:

```
BEFORE                              AFTER
                                    ┌─────────┐
                                    │ Main    │ ← hands out filings,
                                    │ process │   collects results,
                                    └────┬────┘   writes to the database
                                         │
                                         ▼
┌─────────┐                  ┌───┬───┬───┬───┬───┬───┐
│  Main   │                  │ W1│ W2│ W3│ W4│ W5│ W6│ ← workers
│ process │── filing 1 ──▶   │   │   │   │   │   │   │   each gets
│         │── filing 2 ──▶   │   │   │   │   │   │   │   filings to
│         │── filing 3 ──▶   │   │   │   │   │   │   │   work on
│   ...   │                  └───┴───┴───┴───┴───┴───┘
│         │── filing 9000 ▶
└─────────┘                  Core 1  Core 2  …  Core 6  ← OS puts them
1 process,                                                 on physical
~1 core,                                                   cores
5-7 hours
```

The actual extraction work — the call into edgartools that takes the
2-5 seconds — is **identical**. The only thing that changes is **how
many of those calls happen at the same time**.

---

## "Processes" vs "threads" — the one bit of jargon worth knowing

When you want to do things at the same time on a computer, you have
two main options:

- **Threads** = several pieces of work sharing one Python interpreter.
  Lightweight. But Python has a notorious thing called the "GIL"
  (Global Interpreter Lock) that means only one thread can run Python
  bytecode at a time. So for CPU-heavy work like ours, threads don't
  actually run in parallel.
- **Processes** = separate copies of the Python interpreter. Each has
  its own memory, its own GIL. The operating system schedules them
  independently. For CPU-heavy work, processes give true parallelism.

For our workload — edgartools parsing HTML, which is mostly CPU —
processes are the right choice. That's why this is called
**multi*processing*** and not multi*threading*.

---

## The actual code change

Python's standard library has a class called `ProcessPoolExecutor`
that does almost all the work for us. Here is the heart of the
change, simplified:

```python
from concurrent.futures import ProcessPoolExecutor, as_completed

# Old serial loop:
for filing in filings:
    extract_sections(filing)

# New parallel version:
with ProcessPoolExecutor(max_workers=6) as pool:
    futures = [pool.submit(extract_one_filing, f) for f in filings]
    for fut in as_completed(futures):
        result = fut.result()
        # update DB with the result, in the main process
```

What's happening line-by-line:

1. **`ProcessPoolExecutor(max_workers=6)`** — Python asks the operating
   system to create 6 background processes. These are essentially 6
   little Python servers waiting for work.
2. **`pool.submit(extract_one_filing, f)`** — "send this filing to
   whichever worker is free; give me a 'future' (a placeholder for the
   eventual result)."
3. **`as_completed(futures)`** — yields results as soon as any worker
   finishes one, in completion order (not submission order).
4. **`with ... as pool:`** — when the `with` block exits, the pool
   shuts down all 6 worker processes cleanly.

We never touch the OS kernel. We never schedule anything onto cores by
hand. We just tell Python "I want 6 of these," and the OS figures out
the rest.

---

## Why nothing else had to change

Three properties of our Stage 3 design made this almost free to add:

1. **Each filing is independent.** Worker 3 processing CIK 320193 has
   nothing to do with worker 5 processing CIK 1422183. No coordination
   needed.
2. **The extraction function is pure.** `extract_sections(cik,
   accession, raw_root, clean_root)` takes file paths and writes files.
   No shared memory, no global state. A perfect candidate for sending
   off to a worker.
3. **The SQLite database is only written by the main process.** The
   workers don't touch the database. They return small Python
   dictionaries to the main process, which writes them to SQLite
   one-at-a-time. So we never have to worry about two processes
   stepping on each other's database writes.

If any of these had been false, multiprocessing would have required
locks, queues, more careful design. As-is, it's about 30 lines of code
total.

---

## What the user sees

A new CLI flag:

```bash
sibyl sections --workers 6    # use 6 worker processes
sibyl sections --workers 1    # back to serial (handy for debugging)
sibyl sections                # default: min(8, cpu_count - 1)
```

The output is byte-identical regardless of `--workers`. Each filing's
`sections.json`, `risk_factors.txt`, and `mdna.txt` are produced by
the same `extract_sections` function — it just happens to run inside
a worker process now.

---

## What we measured

| Run | Wall clock | CPU usage |
| --- | --- | --- |
| Apple's 10 10-Ks, `--workers 1` | 25.6 sec | 90% (one core) |
| Apple's 10 10-Ks, `--workers 6` | 9.8 sec | 364% (~4 cores worth) |

On 10 filings the speedup is "only" 2.6× because of worker-startup
overhead amortized over a small batch. The full ~9,000-filing pass
should see 4-6× speedup since startup is paid once and the per-filing
work dominates.

---

## When this technique works and when it doesn't

This pattern works well when:

- The work is CPU-bound (not waiting on network/disk)
- Each unit of work is independent
- The work units are similar in size (so workers stay busy)

It works less well when:

- Work is I/O-bound (use async or threads instead — they're lighter)
- Workers need to share lots of memory (processes don't share by default)
- One unit dominates the time (you can't speed up the bottleneck this way)

Stage 3 hits all three "well" criteria perfectly, which is why this
ended up being a small, low-risk change.
