"""Offline source-sync regression tests: local Git fixtures, no builds/downloads."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts/sync-git-patches.sh"


class FetchSourcesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="erbium-fetch-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        home = self.root / "home"
        home.mkdir()
        # Do not depend on (or modify) the developer's identity, hooks or config.
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith("GIT_")}
        self.env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"),
                        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=str(home / ".gitconfig"),
                        GIT_TERMINAL_PROMPT="0", LC_ALL="C")
        self.identity = dict(self.env, GIT_AUTHOR_NAME="Original Author",
                             GIT_AUTHOR_EMAIL="author@example.invalid",
                             GIT_COMMITTER_NAME="Original Committer",
                             GIT_COMMITTER_EMAIL="committer@example.invalid",
                             GIT_AUTHOR_DATE="2001-01-01T00:00:00+0000",
                             GIT_COMMITTER_DATE="2001-01-02T00:00:00+0000")
        self.upstream = self.root / "upstream"
        self.upstream.mkdir()
        self.git(self.upstream, "init", "-q")
        self.git(self.upstream, "config", "core.autocrlf", "false")
        (self.upstream / "seed.txt").write_text("initial\n")
        self.commit(self.upstream, "root", "seed.txt")
        self.ancestor = self.git(self.upstream, "rev-parse", "HEAD").stdout.strip()
        (self.upstream / "seed.txt").write_text("pinned\n")
        self.commit(self.upstream, "pinned base", "seed.txt")
        self.base = self.git(self.upstream, "rev-parse", "HEAD").stdout.strip()
        self.patches = self.root / "patches"
        self.patches.mkdir()
        self.originals = []
        for i in range(1, 6):
            filename = f"feature-{i}.txt"
            (self.upstream / filename).write_text(f"feature {i}\n")
            self.commit(self.upstream, f"patch {i}", filename)
            self.originals.append(self.git(self.upstream, "rev-parse", "HEAD").stdout.strip())
            patch = self.git(self.upstream, "format-patch", "--stdout", "-1").stdout
            (self.patches / f"{i:04d}-feature.patch").write_text(patch)
        self.prefix = self.root / "prefix-patches"
        self.prefix.mkdir()
        for patch in sorted(self.patches.glob("*.patch"))[:4]:
            shutil.copy(patch, self.prefix)
        self.checkout = self.root / "checkout"

    def run_cmd(self, args, *, env=None, check=True):
        result = subprocess.run(args, cwd="/", env=env or self.env,
                                text=True, capture_output=True)
        if check:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def git(self, repo, *args, **kwargs):
        return self.run_cmd(["git", "-C", str(repo), *args], **kwargs)

    def commit(self, repo, message, *paths):
        # Explicit paths only; never stage unrelated fixture contents.
        self.git(repo, "add", "--", *paths)
        self.git(repo, "commit", "-q", "-m", message, env=self.identity)

    def sync(self, *, patches=None, sha=None, url=None, check=True):
        return self.run_cmd(["bash", str(HELPER), str(self.checkout),
                             url or self.upstream.as_uri(), sha or self.base,
                             "fixture-patches", str(patches or self.patches)],
                            check=check)

    def head(self):
        return self.git(self.checkout, "rev-parse", "HEAD").stdout.strip()

    def count(self):
        return int(self.git(self.checkout, "rev-list", "--count",
                            f"{self.base}..HEAD").stdout)

    def original_prefix(self, count=4):
        self.run_cmd(["git", "clone", "-q", "--no-local",
                      self.upstream.as_uri(), str(self.checkout)])
        self.git(self.checkout, "checkout", "-q", "--detach", self.originals[count - 1])

    def snapshot(self):
        """Check rejected updates preserve HEAD, index, worktree and operation state."""
        gitdir = self.checkout / ".git"
        git_files = {}
        for name in ("HEAD", "index", "config", "packed-refs", "shallow", "MERGE_HEAD"):
            path = gitdir / name
            if path.exists():
                git_files[name] = path.read_bytes()
        for name in ("refs", "logs", "rebase-apply", "rebase-merge"):
            directory = gitdir / name
            if directory.exists():
                for path in directory.rglob("*"):
                    if path.is_file():
                        git_files[str(path.relative_to(gitdir))] = path.read_bytes()
        files = {str(path.relative_to(self.checkout)): path.read_bytes()
                 for path in self.checkout.rglob("*")
                 if path.is_file() and ".git" not in path.relative_to(self.checkout).parts}
        return git_files, files

    def refuse(self, message, **kwargs):
        before = self.snapshot()
        result = self.sync(check=False, **kwargs)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(message, result.stderr)
        self.assertEqual(before, self.snapshot())
        return result

    def test_fresh_shallow_clone_applies_entire_series_with_local_identity(self):
        self.sync()
        self.assertEqual(self.count(), 5)
        self.assertEqual(self.git(self.checkout, "rev-parse", "--is-shallow-repository").stdout.strip(), "true")
        self.assertEqual((self.checkout / ".git/shallow").read_text().strip(), self.base)
        self.assertNotEqual(self.head(), self.originals[-1])
        self.assertEqual(self.git(self.checkout, "show", "-s", "--format=%cn <%ce>").stdout.strip(),
                         "erbium-bootstrap <bootstrap@erbium.local>")
        self.assertEqual(self.git(self.checkout, "show", "-s", "--format=%an").stdout.strip(),
                         "Original Author")
        self.assertFalse((self.root / "home/.gitconfig").exists())
        self.assertNotEqual(self.git(self.checkout, "config", "--local", "--get",
                                     "user.name", check=False).returncode, 0)
        self.assertEqual(self.git(self.checkout, "cat-file", "-e", self.ancestor,
                                  check=False).returncode, 1)
        for i in range(1, 6):
            self.assertEqual((self.checkout / f"feature-{i}.txt").read_text(), f"feature {i}\n")

    def test_git_am_prefix_appends_only_fifth_patch(self):
        self.sync(patches=self.prefix)
        old_head = self.head()
        self.assertNotEqual(old_head, self.originals[3])
        self.sync(url="file:///nonexistent/offline-remote")
        self.assertEqual(self.count(), 5)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD^").stdout.strip(), old_head)

    def test_base_only_checkout_applies_all_patches(self):
        self.original_prefix()
        self.git(self.checkout, "checkout", "-q", "--detach", self.base)
        self.sync(url="file:///nonexistent/offline-remote")
        self.assertEqual(self.count(), 5)

    def test_existing_empty_directory_can_be_fetched(self):
        self.checkout.mkdir()
        self.sync()
        self.assertEqual(self.count(), 5)

    def test_global_signing_config_does_not_require_developer_signing_key(self):
        config = self.root / "home/.gitconfig"
        config.write_text("[commit]\n\tgpgSign = true\n[gpg]\n\tprogram = /missing-gpg\n")
        before = config.read_bytes()
        self.sync()
        self.assertEqual(self.count(), 5)
        self.assertEqual(config.read_bytes(), before)

    def test_original_commit_shas_and_dates_are_accepted_as_prefix(self):
        self.original_prefix()
        old_head = self.head()
        self.sync(url="file:///nonexistent/offline-remote")
        self.assertEqual(self.count(), 5)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD^").stdout.strip(), old_head)

    def test_already_current_is_read_only_and_does_not_fetch(self):
        self.sync()
        before = self.snapshot()
        result = self.sync(url="file:///nonexistent/offline-remote")
        self.assertIn("already current", result.stdout)
        self.assertEqual(before, self.snapshot())

    def test_original_full_series_is_already_current(self):
        self.original_prefix(count=5)
        before = self.snapshot()
        self.sync()
        self.assertEqual(self.head(), self.originals[-1])
        self.assertEqual(before, self.snapshot())

    def test_unstaged_tracked_change_refused(self):
        self.sync(patches=self.prefix)
        (self.checkout / "seed.txt").write_text("local modification\n")
        self.refuse("dirty")

    def test_staged_tracked_change_refused(self):
        self.sync(patches=self.prefix)
        (self.checkout / "seed.txt").write_text("staged modification\n")
        self.git(self.checkout, "add", "--", "seed.txt")
        self.refuse("dirty")

    def test_tracked_deletion_refused(self):
        self.sync(patches=self.prefix)
        (self.checkout / "seed.txt").unlink()
        self.refuse("dirty")

    def test_staged_new_file_refused(self):
        self.sync(patches=self.prefix)
        (self.checkout / "local.txt").write_text("staged new file\n")
        self.git(self.checkout, "add", "--", "local.txt")
        self.refuse("dirty")

    def test_untracked_build_artifact_is_preserved(self):
        self.sync(patches=self.prefix)
        (self.checkout / "build").mkdir()
        artifact = self.checkout / "build/cache"
        artifact.write_text("keep me\n")
        self.sync()
        self.assertEqual(artifact.read_text(), "keep me\n")

    def test_custom_commit_refused_before_applying_any_suffix(self):
        self.sync(patches=self.prefix)
        (self.checkout / "custom.txt").write_text("custom\n")
        self.commit(self.checkout, "custom", "custom.txt")
        self.refuse("prefix mismatch")
        self.assertFalse((self.checkout / "feature-5.txt").exists())

    def test_extra_custom_commit_after_full_series_refused(self):
        self.sync()
        (self.checkout / "custom.txt").write_text("extra\n")
        self.commit(self.checkout, "extra", "custom.txt")
        self.refuse("extra/custom commits")

    def test_changed_existing_patch_file_refused(self):
        self.sync(patches=self.prefix)
        path = self.patches / "0001-feature.patch"
        path.write_text(path.read_text().replace("+feature 1\n", "+changed feature 1\n"))
        self.refuse("prefix mismatch")

    def test_changed_existing_commit_refused(self):
        self.original_prefix()
        (self.checkout / "feature-4.txt").write_text("changed committed feature\n")
        self.git(self.checkout, "add", "--", "feature-4.txt")
        self.git(self.checkout, "commit", "--amend", "-q", "--no-edit", env=self.identity)
        self.refuse("prefix mismatch")

    def test_reordered_commits_refused(self):
        self.original_prefix()
        self.git(self.checkout, "checkout", "-q", "--detach", self.base)
        self.git(self.checkout, "cherry-pick", self.originals[1], self.originals[0],
                 env=self.identity)
        self.refuse("prefix mismatch")

    def test_merge_history_refused_even_when_first_parent_matches(self):
        self.original_prefix(count=1)
        self.git(self.checkout, "checkout", "-q", "-b", "side")
        (self.checkout / "side.txt").write_text("side\n")
        self.commit(self.checkout, "side patch", "side.txt")
        self.git(self.checkout, "checkout", "-q", "--detach", self.originals[1])
        self.git(self.checkout, "merge", "-q", "--no-ff", "-m", "merge side", "side",
                 env=self.identity)
        self.refuse("prefix")

    def test_empty_commit_refused(self):
        self.sync(patches=self.prefix)
        self.git(self.checkout, "commit", "--allow-empty", "-q", "-m", "empty",
                 env=self.identity)
        self.refuse("Cannot identify patch")

    def test_wrong_pinned_base_refused(self):
        self.sync(patches=self.prefix)
        # The fifth upstream commit is present but is not this clone's ancestor.
        self.git(self.checkout, "fetch", "-q", self.upstream.as_uri(), self.originals[-1])
        self.refuse("not based on expected pinned base", sha=self.originals[-1])

    def test_missing_pinned_base_refused_without_fetch(self):
        self.sync(patches=self.prefix)
        self.refuse("pinned base", sha="f" * 40, url="file:///nonexistent/offline-remote")

    def test_partial_git_init_has_actionable_non_destructive_error(self):
        self.checkout.mkdir()
        self.git(self.checkout, "init", "-q")
        (self.checkout / "keep.txt").write_text("keep\n")
        result = self.refuse("no HEAD")
        self.assertIn("separate fresh directory", result.stderr)
        self.assertIn("no repair was attempted", result.stderr)

    def test_nonempty_non_git_directory_is_not_adopted(self):
        self.checkout.mkdir()
        (self.checkout / "keep.txt").write_text("keep\n")
        self.refuse("Not a Git checkout")
        self.assertFalse((self.checkout / ".git").exists())

    def test_operation_in_progress_refused(self):
        self.sync(patches=self.prefix)
        (self.checkout / ".git/rebase-apply").mkdir()
        (self.checkout / ".git/rebase-apply/keep").write_text("interrupted operation\n")
        self.refuse("Git operation in progress")

    def test_empty_patch_directory_refused_before_creating_checkout(self):
        empty = self.root / "empty"
        empty.mkdir()
        result = self.sync(patches=empty, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No patches", result.stderr)
        self.assertFalse(self.checkout.exists())

    def test_malformed_patch_refused_before_creating_checkout(self):
        (self.patches / "0005-feature.patch").write_text("not a patch\n")
        result = self.sync(check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Cannot identify a single patch", result.stderr)
        self.assertFalse(self.checkout.exists())

    def test_fetch_script_uses_url_and_pin_overrides_and_updates_cached_backend(self):
        """Exercise the real fetch entry point without preflight/build prerequisites."""
        entry = self.root / "entry checkout"
        scripts = entry / "scripts"
        scripts.mkdir(parents=True)
        shutil.copy(ROOT / "scripts/fetch-sources.sh", scripts)
        shutil.copy(HELPER, scripts)
        # Preflight itself has separate tests; this fixture only exercises fetch.
        preflight = scripts / "preflight.sh"
        preflight.write_text('#!/bin/bash\n[ "$1" = fetch ]\n')
        preflight.chmod(0o755)
        shutil.copytree(self.patches, entry / "qemu-patches")
        shutil.copytree(self.prefix, entry / "sysemu-patches")
        linux = entry / "ext/linux"
        linux.mkdir(parents=True)
        (linux / "keep.txt").write_text("cached Linux untouched\n")
        rootfs = entry / "linux/rootfs"
        rootfs.mkdir(parents=True)
        (rootfs / "busybox").write_text("cached busybox untouched\n")
        env = dict(self.env, QEMU_URL=self.upstream.as_uri(), QEMU_SHA=self.base,
                   ETP_URL=self.upstream.as_uri(), ETP_SHA=self.base)
        command = ["bash", str(scripts / "fetch-sources.sh")]
        self.run_cmd(command, env=env)
        backend = entry / "et-platform"
        qemu = entry / "ext/qemu"
        old_head = self.git(backend, "rev-parse", "HEAD").stdout.strip()
        qemu_head = self.git(qemu, "rev-parse", "HEAD").stdout.strip()
        self.assertFalse((backend / "feature-5.txt").exists())
        shutil.copy(self.patches / "0005-feature.patch", entry / "sysemu-patches")
        # A cached checkout must not contact even the overridden URL on update.
        env.update(QEMU_URL="file:///missing-qemu", ETP_URL="file:///missing-backend")
        self.run_cmd(command, env=env)
        self.assertEqual(self.git(backend, "rev-parse", "HEAD^").stdout.strip(), old_head)
        self.assertEqual(self.git(qemu, "rev-parse", "HEAD").stdout.strip(), qemu_head)
        self.assertEqual((backend / "feature-5.txt").read_text(), "feature 5\n")
        self.assertEqual((linux / "keep.txt").read_text(), "cached Linux untouched\n")
        self.assertEqual((rootfs / "busybox").read_text(), "cached busybox untouched\n")


if __name__ == "__main__":
    unittest.main()
