# Pinned upstream subsystem catalogs

These are inert text snapshots of the upstream root `MAINTAINERS` files.
The source content is unmodified. They retain their upstream copyright and
licensing; no claim is made that these catalogs were authored for this project.

`sources.json` records each source repository, resolved reference, immutable
commit, commit date, retrieval date, source URL, byte count, and SHA-256 digest.
It also records the supporting license files and commit metadata. Linux's
Gitiles transport was base64-decoded to preserve the original file bytes.
The Gitiles anti-XSSI prefix was removed only from its saved commit metadata.

## Linux

- Source: Linux mainline's kernel.googlesource.com mirror
- Commit: `602042bf29f6efde39cfb5fdd9289bf4854bc0c5`
- Commit date: October 7, 2026
- Preserved notices: `linux/COPYING`, `linux/LICENSES/preferred/GPL-2.0`, and
  `linux/LICENSES/exceptions/Linux-syscall-note`

## U-Boot

- Source: the official repository identified by the
  [U-Boot contribution documentation](https://docs.u-boot.org/en/latest/CONTRIBUTE.html)
- Commit: `8d7bc8add17bbc69a58dfe0002eea039dd2dc1bc`
- Commit date: October 5, 2026
- Preserved notices: `u-boot/Licenses/README`, `u-boot/Licenses/gpl-2.0.txt`, and
  `u-boot/Licenses/Exceptions`

Only U-Boot's root `MAINTAINERS` is included. Board-specific or other sections
that exist only in nested `MAINTAINERS` files are outside this catalog. This
snapshot must not be presented as exhaustive board-maintainer coverage.

## Scope and interpretation

These files describe the two source trees at the recorded commits. They do
not update automatically, cover other projects, or establish current ownership
beyond those snapshots. Subsystem names and file paths can overlap between
Linux and U-Boot, so a caller must select the appropriate project catalog.

The v1 classifier evaluates `F:` inclusion and `X:` exclusion file patterns.
It does not evaluate `N:` or `K:` regular expressions, run upstream
`get_maintainer` scripts, inspect history, or claim authoritative ownership.
An absent match is not proof that a path lacks maintainers. A catalog match
does not establish posting, application, rejection, or upstream merge status.

Retrieval used small HTTPS text/metadata requests. No repository or archive
was cloned, no lore web query was made, and no downloaded code was executed.
SHA-256 checks verify the saved bytes against this manifest; upstream Git
object signatures were not verified.
