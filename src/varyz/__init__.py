"""Varyz — a self-filling, local-first memory of your entire life."""

__version__ = "0.1.7"

# The attribution term the licence supplements the AGPL with is a requirement to
# *display* a notice, not merely to ship a file, so it has to be reachable from
# the program itself.  It lives here rather than in the command line, because
# anything importing Varyz as a library never runs `varyz --version` and still
# has to be able to show it.  The wording is the one in
# LICENSE.additional-terms verbatim: an approximation of it would not satisfy
# the term it exists to satisfy.
LEGAL_NOTICE = """Based on varyzai — Copyright (C) 2026 Egor Menshikov
Source: https://github.com/egrm07/varyz

Licensed under the GNU Affero General Public License version 3 with the
additional terms permitted by its section 7, stated in the file
LICENSE.additional-terms. This program comes with ABSOLUTELY NO WARRANTY.

'Varyz' and 'varyzai' are the project's names and are not licensed to you;
a fork is free to take the code and must choose its own name."""

# Before 2026-09-24 every variable was spelled VARYS_*. An owner's shell profile
# and an MCP entry written by the old installer still say so; importing the
# package is the one point every entry -- CLI, MCP server, hooks, daemon -- passes.
from varyz.legacy import adopt_legacy_environment  # noqa: E402

adopt_legacy_environment()

__all__ = ["LEGAL_NOTICE", "__version__"]
