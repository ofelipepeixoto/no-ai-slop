"""Behavior checks for package validation and preservation of local artifacts."""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
EXPECTED = {
    '.codex-plugin/plugin.json', 'assets/no-ai-slop.png',
    'skills/no-ai-slop/SKILL.md', 'skills/no-ai-slop/eval.md',
    'LICENSE', 'PRIVACY.md', 'TERMS.md',
}


class BuilderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='no-ai-slop-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'repo'
        shutil.copytree(SOURCE, self.root, ignore=shutil.ignore_patterns('.git', 'dist', '__pycache__', 'tests'))
        self.manifest = self.root / '.codex-plugin/plugin.json'
        self.archive = self.root / 'dist/no-ai-slop-plugin-1.0.6.zip'

    def run_build(self, check=False):
        return subprocess.run(
            [sys.executable, 'scripts/build_plugin.py', *(['--check'] if check else [])],
            cwd=self.root, capture_output=True, text=True, timeout=10,
        )

    def mutate(self, change):
        manifest = json.loads(self.manifest.read_text())
        change(manifest)
        self.manifest.write_text(json.dumps(manifest))

    def assert_rejected(self, change):
        self.mutate(change)
        result = self.run_build()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse((self.root / 'dist').exists(), 'Invalid metadata must fail before writes')

    def load_builder(self):
        specification = importlib.util.spec_from_file_location('fixture_builder', self.root / 'scripts/build_plugin.py')
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        return module

    def test_build_has_exact_sources_and_bytes(self):
        result = self.run_build()
        self.assertEqual(result.returncode, 0, result.stderr)
        with zipfile.ZipFile(self.archive) as archive:
            self.assertEqual(set(archive.namelist()), {f'no-ai-slop/{name}' for name in EXPECTED})
            self.assertEqual(len(archive.namelist()), len(EXPECTED))
            for name in EXPECTED:
                self.assertEqual(archive.read(f'no-ai-slop/{name}'), (self.root / name).read_bytes())

    def test_check_leaves_no_dist(self):
        result = self.run_build(check=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / 'dist').exists())

    def test_check_preserves_existing_build_and_extra_file(self):
        self.assertEqual(self.run_build().returncode, 0)
        sentinel = self.root / 'dist/no-ai-slop/keep.txt'
        sentinel.write_bytes(b'important local build artifact')
        before = {str(path.relative_to(self.root)): path.read_bytes() for path in (self.root / 'dist').rglob('*') if path.is_file()}
        result = self.run_build(check=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        after = {str(path.relative_to(self.root)): path.read_bytes() for path in (self.root / 'dist').rglob('*') if path.is_file()}
        self.assertEqual(before, after)

    def test_rejects_prompt_as_string(self):
        self.assert_rejected(lambda m: m['interface'].__setitem__('defaultPrompt', 'abc'))

    def test_rejects_prompt_as_mapping(self):
        self.assert_rejected(lambda m: m['interface'].__setitem__('defaultPrompt', {'a': 'starter'}))

    def test_rejects_author_as_string(self):
        self.assert_rejected(lambda m: m.__setitem__('author', 'not-an-object'))

    def test_rejects_missing_skills_reference(self):
        self.assert_rejected(lambda m: m.__setitem__('skills', './missing-skills/'))

    def test_rejects_icon_outside_package(self):
        self.assert_rejected(lambda m: m['interface'].__setitem__('composerIcon', '../outside.png'))

    def test_rejects_capabilities_as_string(self):
        self.assert_rejected(lambda m: m['interface'].__setitem__('capabilities', 'Edit'))

    def test_rejects_numeric_version(self):
        self.assert_rejected(lambda m: m.__setitem__('version', 123))

    def test_rejects_conditional_version_traversal_and_preserves_target(self):
        outside = self.root / 'outside.zip'
        outside.write_bytes(b'keep this file outside dist')
        prefix = self.root / 'dist/no-ai-slop-plugin-1.0.6'
        prefix.mkdir(parents=True)
        self.mutate(lambda m: m.__setitem__('version', '1.0.6/../../outside'))
        result = self.run_build()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(outside.read_bytes(), b'keep this file outside dist')
        self.assertTrue(prefix.is_dir())
        self.assertFalse((self.root / 'dist/no-ai-slop').exists())

    def test_rejects_four_prompts(self):
        self.assert_rejected(lambda m: m['interface'].__setitem__('defaultPrompt', ['a', 'b', 'c', 'd']))

    def test_rejects_prompt_longer_than_128(self):
        self.assert_rejected(lambda m: m['interface'].__setitem__('defaultPrompt', ['x' * 129]))

    def test_rejects_empty_or_nonstring_prompt(self):
        for prompt in ('   ', 123):
            with self.subTest(prompt=prompt):
                self.assert_rejected(lambda m: m['interface'].__setitem__('defaultPrompt', [prompt]))

    def test_accepts_prerelease_and_build_semver(self):
        self.mutate(lambda m: m.__setitem__('version', '1.2.3-rc.1+build.001'))
        result = self.run_build()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / 'dist/no-ai-slop-plugin-1.2.3-rc.1+build.001.zip').is_file())

    def test_rejects_noncanonical_semver(self):
        for version in ('01.2.3', '1.2.3-01', '1.2', '1.2.3/bad', '1.2.3\n'):
            with self.subTest(version=version):
                self.assert_rejected(lambda m: m.__setitem__('version', version))

    def test_missing_license_fails_before_dist_write(self):
        (self.root / 'LICENSE').unlink()
        result = self.run_build()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'dist').exists())

    def test_rejects_manifest_array(self):
        self.manifest.write_text('["not an object"]')
        result = self.run_build()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'dist').exists())

    def test_rejects_modified_archive_payload(self):
        self.assertEqual(self.run_build().returncode, 0)
        with zipfile.ZipFile(self.archive) as package:
            contents = {name: package.read(name) for name in package.namelist()}
        contents['no-ai-slop/LICENSE'] = b'replaced license'
        with zipfile.ZipFile(self.archive, 'w') as package:
            for name, data in contents.items():
                package.writestr(name, data)
        builder = self.load_builder()
        with self.assertRaisesRegex(SystemExit, 'Archive LICENSE'):
            builder.validate_build(self.root / 'dist/no-ai-slop', self.archive)

    def test_rejects_duplicate_archive_member(self):
        self.assertEqual(self.run_build().returncode, 0)
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with zipfile.ZipFile(self.archive, 'a') as package:
                package.writestr('no-ai-slop/LICENSE', (self.root / 'LICENSE').read_bytes())
        builder = self.load_builder()
        with self.assertRaisesRegex(SystemExit, 'duplicate members'):
            builder.validate_build(self.root / 'dist/no-ai-slop', self.archive)

    def test_rejects_unexpected_archive_member(self):
        self.assertEqual(self.run_build().returncode, 0)
        with zipfile.ZipFile(self.archive, 'a') as package:
            package.writestr('no-ai-slop/extra.txt', b'extra')
        builder = self.load_builder()
        with self.assertRaisesRegex(SystemExit, 'unexpected'):
            builder.validate_build(self.root / 'dist/no-ai-slop', self.archive)


if __name__ == '__main__':
    unittest.main()
