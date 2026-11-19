import hashlib
import importlib.util
import json
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('portal_release_builder', ROOT / 'scripts/build_release.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.result = builder.build_release(ROOT, self.directory, 'test-v1')
        self.package = Path(self.result['package'])

    def tearDown(self):
        self.temp.cleanup()

    def run_updater(self, *args):
        powershell = shutil.which('powershell.exe') or shutil.which('pwsh')
        if not powershell:
            self.skipTest('PowerShell not installed')
        return subprocess.run([powershell, '-NoProfile', '-File', str(ROOT / 'scripts/update.ps1'),
                               '-Package', str(self.package), *map(str, args)],
                              text=True, capture_output=True, timeout=40, errors='replace')

    def tamper(self, name, data):
        with zipfile.ZipFile(self.package) as archive:
            content = {item: archive.read(item) for item in archive.namelist()}
        content[name] = data
        with zipfile.ZipFile(self.package, 'w') as archive:
            for path, value in content.items():
                archive.writestr(path, value)
        Path(str(self.package) + '.sha256').write_text(hashlib.sha256(self.package.read_bytes()).hexdigest())

    def test_code_only_package_and_complete_manifest(self):
        with zipfile.ZipFile(self.package) as archive:
            names = archive.namelist()
            self.assertNotIn('.env', names)
            self.assertFalse(any(name.startswith(('data/', 'tests/', '.release-')) for name in names))
            self.assertIn('app/static/portal.css', names)
            manifest = json.loads(archive.read('release-manifest.json'))
            for item in manifest['files']:
                self.assertEqual(hashlib.sha256(archive.read(item['path'])).hexdigest(), item['sha256'])
        result = self.run_updater('-ValidateOnly')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('package=ok', result.stdout)

    def test_install_preserves_server_environment_and_data(self):
        target = self.directory / 'teacher-funding-portal'
        (target / 'app').mkdir(parents=True)
        (target / 'app/obsolete.py').write_text('old code')
        (target / '.env').write_text('SERVER_SECRET=unchanged')
        (target / 'data').mkdir()
        (target / 'data/portal.sqlite3').write_bytes(b'database-do-not-change')
        result = self.run_updater('-TargetRoot', target, '-InstallOnly')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((target / '.env').read_text(), 'SERVER_SECRET=unchanged')
        self.assertEqual((target / 'data/portal.sqlite3').read_bytes(), b'database-do-not-change')
        self.assertFalse((target / 'app/obsolete.py').exists())
        self.assertTrue(list((target / '.release-backups').glob('*/app/obsolete.py')))
        # A second operation must be able to reacquire the update lock.
        again = self.run_updater('-TargetRoot', target, '-InstallOnly')
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)

    def test_tampered_code_and_forbidden_entries_are_rejected(self):
        self.tamper('app/main.py', b'modified')
        self.assertNotEqual(self.run_updater('-ValidateOnly').returncode, 0)

    def test_forbidden_env_entry_is_rejected(self):
        self.tamper('.env', b'secret')
        self.assertNotEqual(self.run_updater('-ValidateOnly').returncode, 0)

    def test_traversal_entry_is_rejected(self):
        self.tamper('app/../../outside.py', b'bad')
        self.assertNotEqual(self.run_updater('-ValidateOnly').returncode, 0)

    def test_build_failure_restores_existing_code(self):
        powershell = shutil.which('powershell.exe') or shutil.which('pwsh')
        if not powershell:
            self.skipTest('PowerShell not installed')
        target = self.directory / 'teacher-funding-portal'
        (target / 'app').mkdir(parents=True)
        (target / 'app/main.py').write_text('old-code-kept')
        (target / 'compose.yml').write_text('old-compose-kept')
        (target / '.env').write_text('server-kept')
        harness = self.directory / 'simulate-failure.ps1'
        def quote(path):
            return "'" + str(path).replace("'", "''") + "'"
        harness.write_text("function global:docker { $global:LASTEXITCODE=0; if ($args -contains 'build') { $global:LASTEXITCODE=1 } }\n"
                           + '& ' + quote(ROOT / 'scripts/update.ps1') + ' -Package ' + quote(self.package)
                           + ' -TargetRoot ' + quote(target), encoding='ascii')
        result = subprocess.run([powershell, '-NoProfile', '-File', str(harness)], capture_output=True, timeout=40)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((target / 'app/main.py').read_text(), 'old-code-kept')
        self.assertEqual((target / 'compose.yml').read_text(), 'old-compose-kept')
        self.assertEqual((target / '.env').read_text(), 'server-kept')


if __name__ == '__main__':
    unittest.main()
