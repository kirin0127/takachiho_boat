"""Exercise the actual workflow shell in disposable Git repos and fake systemd."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

WORKFLOW = Path(__file__).resolve().parent / '.github' / 'workflows' / 'deploy.yml'
WORKFLOW_TEXT = WORKFLOW.read_text()
REMOTE_BODY = WORKFLOW_TEXT.split("            bash -se <<'BOAT_DEPLOY'\n", 1)[1].rsplit('            BOAT_DEPLOY', 1)[0]
SCRIPT = '\n'.join(line[12:] for line in REMOTE_BODY.splitlines()) + '\n'
SUDOERS = Path(__file__).resolve().parent / 'deploy' / 'deploy.sudoers'


def run(*command, cwd=None, env=None):
    return subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True, check=True).stdout.strip()


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.origin = self.root / 'origin'
        self.checkout = self.root / 'boat'
        self.etc = self.root / 'etc'
        self.units = self.etc / 'systemd/system'
        self.units.mkdir(parents=True)
        self.origin.mkdir()
        run('git', 'init', '-q', '-b', 'main', cwd=self.origin)
        (self.origin / '.gitignore').write_text('__pycache__/\n.env\nstate/\n')
        (self.origin / 'deploy').mkdir()
        for unit in ('service', 'timer'):
            (self.origin / f'deploy/takachiho-boat.{unit}').write_text('old-unit\n')
            (self.units / f'takachiho-boat.{unit}').write_text('old-unit\n')
        (self.origin / 'test_smoke.py').write_text('import unittest\nclass Check(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n')
        self.commit('old')
        self.previous = run('git', 'rev-parse', 'HEAD', cwd=self.origin)
        run('git', 'clone', '-q', str(self.origin), str(self.checkout))
        for unit in ('service', 'timer'):
            (self.origin / f'deploy/takachiho-boat.{unit}').write_text('new-unit\n')
        (self.origin / 'version.txt').write_text('new\n')
        self.commit('new')
        self.candidate = run('git', 'rev-parse', 'HEAD', cwd=self.origin)
        (self.etc / 'takachiho-boat.env').write_text('test-config-unchanged\n')
        (self.checkout / 'state').mkdir()
        (self.checkout / 'state/notifications.json').write_text('test-state-unchanged\n')
        overrides = self.units / 'takachiho-boat.timer.d'
        overrides.mkdir()
        (overrides / 'override.conf').write_text('OnUnitInactiveSec=60s\n')
        self.active = self.root / 'active'
        self.active.touch()
        binaries = self.root / 'bin'
        binaries.mkdir()
        self.write_executable(binaries / 'id', '#!/bin/sh\necho "${DEPLOY_TEST_UID:-1001}"\n')
        self.write_executable(binaries / 'sudo', r'''#!/usr/bin/env python3
import os, pathlib, sys
args = sys.argv[1:]
if not args or args.pop(0) != '-n': sys.exit('Expected non-interactive sudo')
rules = pathlib.Path(os.environ['DEPLOY_TEST_SUDOERS']).read_text().splitlines()
allowed = {line.split('NOPASSWD: ', 1)[1] for line in rules if 'NOPASSWD: ' in line}
command = '/usr/bin/' + ' '.join(args)
if command not in allowed: sys.exit('Not permitted by deploy.sudoers: ' + command)
os.execvp(args[0], args)
''')
        for name in ('journalctl', 'systemd-analyze'):
            self.write_executable(binaries / name, '#!/bin/sh\nexit 0\n')
        self.write_executable(binaries / 'systemctl', r'''#!/usr/bin/env python3
import json, os, pathlib, sys
root=pathlib.Path(os.environ['DEPLOY_TEST_ROOT'])
args=sys.argv[1:]
with (root/'calls').open('a') as out: out.write(json.dumps(args)+'\n')
active=root/'active'
if args[0]=='is-active': sys.exit(0 if active.exists() else 3)
if args[0]=='stop' and 'takachiho-boat.timer' in args: active.unlink(missing_ok=True)
if args[0]=='start' and 'takachiho-boat.service' in args and os.environ.get('FAIL_LIVE'): sys.exit(42)
if args[0] in ('start','enable') and 'takachiho-boat.timer' in args: active.touch()
''')
        self.env = dict(os.environ, PATH=str(binaries) + ':' + os.environ['PATH'],
                        DEPLOY_SHA=self.candidate, DEPLOY_TEST_ROOT=str(self.root),
                        DEPLOY_TEST_SUDOERS=str(SUDOERS))
        self.script = self.root / 'deploy.sh'
        self.script.write_text(SCRIPT.replace('/opt/boat', str(self.checkout)).replace('/etc/', str(self.etc) + '/'))

    def write_executable(self, path, text):
        path.write_text(text)
        path.chmod(0o755)

    def commit(self, message):
        run('git', 'add', '.', cwd=self.origin)
        run('git', '-c', 'user.name=Deployment test', '-c', 'user.email=test@example.invalid',
            '-c', 'commit.gpgsign=false', 'commit', '-qm', message, cwd=self.origin)

    def deploy(self):
        return subprocess.run(['bash', str(self.script)], env=self.env, capture_output=True, text=True)

    def assert_preserved(self):
        self.assertEqual((self.etc/'takachiho-boat.env').read_text(), 'test-config-unchanged\n')
        self.assertEqual((self.checkout/'state/notifications.json').read_text(), 'test-state-unchanged\n')
        self.assertEqual((self.units/'takachiho-boat.timer.d/override.conf').read_text(), 'OnUnitInactiveSec=60s\n')

    def test_success_deploys_exact_commit_and_preserves_config_state_override(self):
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(run('git', 'rev-parse', 'HEAD', cwd=self.checkout), self.candidate)
        self.assertEqual((self.units/'takachiho-boat.service').read_text(), 'old-unit\n')
        self.assertTrue(self.active.exists())
        self.assert_preserved()

    def test_root_login_remains_supported(self):
        self.env['DEPLOY_TEST_UID'] = '0'
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(run('git', 'rev-parse', 'HEAD', cwd=self.checkout), self.candidate)

    def test_failed_live_check_rolls_back_and_restores_active_timer(self):
        self.env['FAIL_LIVE'] = '1'
        result = self.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('restoring commit', result.stdout)
        self.assertEqual(run('git', 'rev-parse', 'HEAD', cwd=self.checkout), self.previous)
        self.assertEqual((self.units/'takachiho-boat.service').read_text(), 'old-unit\n')
        self.assertTrue(self.active.exists())
        self.assert_preserved()

    def test_candidate_test_failure_does_not_stop_timer_or_replace_code(self):
        (self.origin/'test_smoke.py').write_text('import unittest\nclass Check(unittest.TestCase):\n def test_bad(self): self.fail("bad candidate")\n')
        self.commit('broken')
        self.env['DEPLOY_SHA'] = run('git', 'rev-parse', 'HEAD', cwd=self.origin)
        result = self.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(run('git', 'rev-parse', 'HEAD', cwd=self.checkout), self.previous)
        self.assertNotIn('"stop"', (self.root/'calls').read_text())
        self.assertTrue(self.active.exists())

    def test_superseded_commit_is_skipped(self):
        self.env['DEPLOY_SHA'] = self.previous
        result = self.deploy()
        self.assertEqual(result.returncode, 0)
        self.assertIn('Skipping', result.stdout)
        self.assertEqual(run('git', 'rev-parse', 'HEAD', cwd=self.checkout), self.previous)

    def test_uncommitted_vps_edits_are_not_overwritten(self):
        (self.checkout/'local-note.txt').write_text('preserve me')
        result = self.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('local changes', result.stdout)
        self.assertEqual((self.checkout/'local-note.txt').read_text(), 'preserve me')
        self.assertTrue(self.active.exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
