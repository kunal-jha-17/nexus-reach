"""
parallel.py -- do one slow step (open a website, ask the AI) for many leads at once.

Each lead's work runs in a worker thread with the caller's context (whose account, which
database), and the results come back to the caller's thread to be saved -- so database writes,
logging and progress stay exactly as they were when everything ran one at a time.
"""
import contextvars
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait


def run(ctx, items, work, on_result, workers=4):
    """Call work(item) for every item, `workers` at a time; on_result(index, item, result, error)
    in the calling thread as each finishes. Stops handing out new items once ctx.stopped is set.
    Returns how many items were started."""
    items = list(items)
    workers = max(1, int(workers or 1))
    if workers == 1:
        n = 0
        for i, item in enumerate(items):
            if getattr(ctx, "stopped", False):
                break
            n += 1
            try:
                res, err = work(item), None
            except Exception as e:  # noqa: BLE001 -- one item's failure is reported, not raised
                res, err = None, e
            on_result(i, item, res, err)
        return n
    started = 0
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="lead-work") as pool:
        pending = {}
        it = iter(enumerate(items))

        def submit():
            nonlocal started
            while len(pending) < workers * 2 and not getattr(ctx, "stopped", False):
                try:
                    i, item = next(it)
                except StopIteration:
                    return
                fut = pool.submit(contextvars.copy_context().run, work, item)
                pending[fut] = (i, item)
                started += 1

        submit()
        while pending:
            done, _ = wait(list(pending), return_when=FIRST_COMPLETED)
            for fut in done:
                i, item = pending.pop(fut)
                err = fut.exception()
                on_result(i, item, None if err else fut.result(), err)
            submit()
    return started
