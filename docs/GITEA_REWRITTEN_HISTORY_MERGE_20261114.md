# Gitea rewritten history integration

The remote history was rewritten and the application renamed to DevWisp. Integrate the fetched Gitea snapshot without restarting the running service or pushing changes.

1. Back up the current branch and stash the two pre-existing launcher deletions.
2. Bridge the last integrated upstream snapshot (3a45bce) to the rewritten upstream snapshot, then merge its changes into main. Preserve both histories.
3. Resolve renamed modules against upstream, migrate local Windows port 26546 and duplicate-start protection, preserve local runtime fixes, reuse the existing Windows data directory, and support shared workspace dependencies. Keep existing user data intact.
4. Build with pnpm, run relevant existing backend checks, inspect conflicts and whitespace, and commit locally.
5. Keep both existing user data directories and their separate databases untouched; note that existing data needs a deliberate follow-up migration. Leave the two pre-existing launcher deletions intact in the stash because upstream removed those files. Do not restart or push.

Classification: upstream feature and naming migration; retain the upstream release version.
