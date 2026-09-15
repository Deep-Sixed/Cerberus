"""Reject known private deployment material in all ancestry proposed for release.

Print commit/path/rule identifiers only, never matching content. This complements
Gitleaks; it is not a general secret detector or a substitute for manual review.
"""
import re
import subprocess
import sys

PRIVATE_PATHS = {
    "AUDIT-2026-07-17.md", "AUDIT-2026-07-18.md",
    "SESSION-AUDIT-2026-07-18-CLAUDE.md", "deploy/SSO-ACTIVATION-SESSION-2026-07-20.md",
    "config/friday.yaml", "config/production.yaml", "deploy/admin-sso-setup.md",
    "PLAN.md", "SPEC.md",
    "deploy/contextforge-ingest-v3.md",
}
# These literals are split so the scanner does not match its own source.
PATTERNS = {
    "operator-vault": re.compile(rb"jarvis-" + rb"secret", re.I),
    "operator-client": re.compile(rb"(?:CB_KEY_|cb-)" + rb"fri" + rb"day", re.I),
    "private-bridge": re.compile(rb"host\.docker\." + rb"internal"),
    "operator-deployment": re.compile(rb"EVECOR/" + rb"gateway/cerberus"),
    "operator-home": re.compile(rb"/home/" + rb"jarvis/"),
    "operator-storage": re.compile(rb"/mnt/" + rb"jarvis-data/"),
    "private-ip": re.compile(rb"\b(?:10\.(?:\d{1,3}\.){2}\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})\b"),
    # the publishing identity is Deep-Sixed; the maintainer's personal name,
    # account and address must not appear in any published blob or message
    "personal-identity": re.compile(rb"charles" + rb"\s*\.?\s*snyder" + rb"|charles" + rb"snyder", re.I),
}


def git(*args):
    return subprocess.check_output(["git", *args])


def main():
    seen = set()
    findings = 0
    commits = git("rev-list", "--all", "HEAD").decode().splitlines()
    for commit in commits:
        message = git("show", "-s", "--format=%B", commit)
        for rule, pattern in PATTERNS.items():
            if pattern.search(message):
                print(commit, "<commit-message>", rule)
                findings += 1
        for record in git("ls-tree", "-rz", commit).split(b"\0"):
            if not record:
                continue
            metadata, raw_path = record.split(b"\t", 1)
            _, kind, oid = metadata.split()
            path = raw_path.decode()
            if kind != b"blob" or (path, oid) in seen:
                continue
            seen.add((path, oid))
            if path in PRIVATE_PATHS or path.endswith((".kdbx", ".p12", ".pfx")):
                print(commit, path, "private-path")
                findings += 1
            data = git("cat-file", "blob", oid.decode())
            for rule, pattern in PATTERNS.items():
                if pattern.search(data):
                    print(commit, path, rule)
                    findings += 1
    print(f"Checked {len(commits)} commits, {len(seen)} path/blob pairs; {findings} findings")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
