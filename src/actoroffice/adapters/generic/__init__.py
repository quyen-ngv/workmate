"""Generic adapters: connect an outside system by configuration only (`use: http | mcp | command`).

Each one is a document source (`DocSource`): a document is some text addressed by a reference. What a reference looks
like, how one is fetched and how the answer is read all come from the connection's config.
"""
