# sp500_membership.csv

Point-in-time S&P 500 membership intervals, 1996-01-02 through 2026-08-18.
`end` empty = still a member at the end of the source history.

Derived (by diffing consecutive daily snapshots) from
https://github.com/fja05680/sp500 — "S&P 500 Historical Components & Changes (Updated).csv",
MIT License, Copyright (c) 2019-2020 Farrell J. Aultman. That file originates from the
dataset distributed with Andreas Clenow's *Trading Evolved* (1996-2019) and is maintained
against Wikipedia's "Selected changes" table since.

Caveats from the source: the first ~5 years may be missing a handful of symbols
(487 symbols on 1996-01-02; never below 494 after 2001-01-16). Symbols are as traded
on the day; a renamed company appears as a removal of the old symbol and an addition of
the new one. Membership after the source's last date is extended by `universe refresh`
from the current constituents list and marked `source = observed`.
