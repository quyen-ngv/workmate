"""The documents repository: every document, ticket card and knowledge item of the office as a Markdown file in a git
repository, so the whole can be browsed, grepped, reviewed and edited with ordinary tools.

`layout` says where each thing sits and how a file is written and read; `chunks` cuts a file into the pieces the
full-text index holds. Both are pure (no I/O). `services/docrepo.py` keeps the folder and the office in step.
"""
