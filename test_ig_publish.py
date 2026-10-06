import importlib.util
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('publisher', Path(__file__).with_name('ig_publish.py'))
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.row = dict(post_id='test-1', release_at='2026-01-01 00:00:00',
                        caption='Cat earrings', images='https://cdn.example.com/cat.jpg', status='queued')
        self.now = datetime(2026, 10, 6, tzinfo=timezone.utc)
        self.state = {}
        self.events = []

    def checkpoint(self, state):
        self.events.append(('saved', json.loads(json.dumps(state))))

    def api(self, method, path, **params):
        self.events.append((path, params))
        if path.endswith('/media_publish'):
            self.assertEqual(self.events[-2][1]['test-1']['phase'], 'publishing')
            return {'id': 'public-1'}
        if path.endswith('/media'):
            return {'id': 'container-1'}
        if params.get('fields') == 'permalink':
            return {'permalink': 'https://www.instagram.com/p/test/'}
        return {'status_code': 'FINISHED'}

    def run_publish(self, api=None):
        with patch.object(p, 'call', side_effect=api or self.api), patch.object(p, 'checkpoint', side_effect=self.checkpoint):
            p.publish('account-1', self.row, self.state)

    def test_checkpoint_precedes_publication(self):
        self.run_publish()
        self.assertEqual(self.state['test-1']['phase'], 'published')
        self.run_publish()
        self.assertEqual(sum(e[0].endswith('/media_publish') for e in self.events), 1)

    def test_legacy_recovery_is_not_due(self):
        for entry in ({'posted_at': '2026-10-01T00:00:00+00:00'}, {'permalink': 'https://instagram.com/p/x/'}, {'media_id': '123'}):
            self.assertFalse(p.eligible(self.row, {'test-1': entry}, self.now))

    def test_timeout_remains_blocked_after_restart(self):
        def api(method, path, **params):
            if path.endswith('/media_publish'):
                raise TimeoutError('lost response')
            return self.api(method, path, **params)
        with self.assertRaises(TimeoutError):
            self.run_publish(api)
        self.state = json.loads(json.dumps(self.state))
        self.assertEqual(self.state['test-1']['phase'], 'publishing')
        self.assertFalse(p.eligible(self.row, self.state, self.now))
        before = len(self.events)
        self.run_publish()
        self.assertEqual(len(self.events), before)

    def test_checkpoint_failure_prevents_publication(self):
        def save(state):
            if state['test-1']['phase'] == 'publishing':
                raise RuntimeError('push failed')
        with patch.object(p, 'call', side_effect=self.api), patch.object(p, 'checkpoint', side_effect=save):
            with self.assertRaises(RuntimeError):
                p.publish('account-1', self.row, self.state)
        self.assertFalse(any(e[0].endswith('/media_publish') for e in self.events))

    def test_missing_media_id_is_ambiguous(self):
        def api(method, path, **params):
            return {} if path.endswith('/media_publish') else self.api(method, path, **params)
        with self.assertRaises(RuntimeError):
            self.run_publish(api)
        self.assertEqual(self.state['test-1']['phase'], 'publishing')

    def test_permalink_failure_does_not_retry_post(self):
        def api(method, path, **params):
            if params.get('fields') == 'permalink':
                raise RuntimeError('temporary read failure')
            return self.api(method, path, **params)
        self.run_publish(api)
        self.assertEqual(self.state['test-1']['media_id'], 'public-1')
        self.assertTrue(self.state['test-1']['link_pending'])
        self.assertFalse(p.eligible(self.row, self.state, self.now))

    def test_changed_caption_cannot_reuse_container(self):
        self.state['test-1'] = {'container_id': 'prepared', 'fingerprint': p.fingerprint(self.row), 'phase': 'ready'}
        self.row['caption'] = 'Changed'
        with self.assertRaises(RuntimeError):
            self.run_publish()
        self.assertEqual(self.events, [])

    def test_wrong_account_cannot_reuse_container(self):
        self.state['test-1'] = {'container_id': 'prepared', 'account_id': 'other'}
        with self.assertRaises(RuntimeError):
            self.run_publish()
        self.assertEqual(self.events, [])

    def test_reels_uses_verified_video(self):
        self.row.update(media_type='REELS', images='', video_url='https://cdn.example.com/a.mp4', video_sha256='a' * 64)
        with patch.object(p, 'verify_video') as verify:
            self.run_publish()
        verify.assert_called_once_with(self.row)
        creation = [e[1] for e in self.events if e[0] == 'account-1/media'][0]
        self.assertEqual(creation['media_type'], 'REELS')
        self.assertEqual(creation['video_url'], self.row['video_url'])

    def test_video_hash_failure_prevents_meta_calls(self):
        self.row.update(media_type='REELS', images='', video_url='https://cdn.example.com/a.mp4', video_sha256='a' * 64)
        with patch.object(p, 'verify_video', side_effect=ValueError('changed')):
            with self.assertRaises(ValueError):
                self.run_publish()
        self.assertEqual(self.events, [])

    def test_carousel_failure_does_not_silently_drop_images(self):
        self.row['images'] += '|https://cdn.example.com/b.jpg'
        with self.assertRaises(p.MetaAPIError):
            self.run_publish(lambda *args, **kwargs: (_ for _ in ()).throw(p.MetaAPIError('download failed', 9004, 2207052)))
        self.assertFalse(p.confirmed(self.state['test-1']))

    def test_carousel_contains_all_children(self):
        self.row['images'] += '|https://cdn.example.com/b.jpg'
        self.run_publish()
        parents = [e[1] for e in self.events if e[0] == 'account-1/media' and e[1].get('media_type') == 'CAROUSEL']
        self.assertEqual(parents[0]['children'], 'container-1,container-1')

    def test_rejects_invalid_media(self):
        for url in ('http://cdn.example.com/a.jpg', 'https://user:pass@cdn.example.com/a.jpg', '', 'https://127.0.0.1/a.jpg', 'https://localhost/a.jpg'):
            self.row['images'] = url
            with self.assertRaises(ValueError):
                p.validate_row(self.row)

    def test_no_silent_truncation(self):
        self.row['images'] = '|'.join('https://cdn.example.com/a.jpg' for _ in range(11))
        with self.assertRaises(ValueError):
            p.validate_row(self.row)

    def test_published_container_never_republished(self):
        self.state['test-1'] = {'container_id': 'prepared', 'phase': 'ready'}
        with patch.object(p, 'wait_ready', return_value='PUBLISHED'):
            with self.assertRaises(RuntimeError):
                self.run_publish()
        self.assertEqual(self.state['test-1']['phase'], 'needs_review')
        self.assertFalse(any(e[0].endswith('/media_publish') for e in self.events))

    def test_atomic_state_is_readable(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(p, 'STATE', str(Path(directory) / 'state.json')), patch.object(p, 'DURABLE_GIT', False):
            p.checkpoint({'test-1': {'phase': 'publishing'}})
            self.assertEqual(json.loads(Path(p.STATE).read_text())['test-1']['phase'], 'publishing')
            self.assertFalse(Path(p.STATE + '.tmp').exists())

    def test_dry_run_has_no_network_or_state_writes_even_with_token(self):
        with tempfile.TemporaryDirectory() as directory:
            queue = Path(directory) / 'queue.csv'
            queue.write_text('post_id,release_at,caption,images,status\ntest-1,2026-01-01 00:00:00,Cat,https://cdn.example.com/cat.jpg,queued\n')
            with patch.object(p, 'QUEUE', str(queue)), patch.object(p, 'STATE', str(Path(directory) / 'missing.json')), patch.object(p, 'DRY_RUN', True), patch.object(p, 'TOKEN', 'test-secret'), patch.object(p, 'call') as api, patch.object(p, 'checkpoint') as save:
                self.assertEqual(p.main(), 0)
                api.assert_not_called()
                save.assert_not_called()

    def test_git_failure_never_exposes_output(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(p, 'STATE', str(Path(directory) / 'state.json')), patch.object(p, 'DURABLE_GIT', True), patch.object(p.subprocess, 'run') as run:
            run.return_value.returncode = 128
            with self.assertRaisesRegex(RuntimeError, 'persist publication state') as error:
                p.checkpoint({})
            self.assertNotIn('secret', str(error.exception))

    def test_video_requires_explicit_host_prefix(self):
        self.row.update(video_url='https://media.example.com/videos/a.mp4', video_sha256='a' * 64)
        for prefix in ('', 'https://media.example.com.evil/videos', 'https://media.example.com/video'):
            with patch.dict(p.os.environ, {'IG_VIDEO_PREFIX': prefix}), patch.object(p.urllib.request, 'build_opener') as opener:
                with self.assertRaises(ValueError):
                    p.verify_video(self.row)
                opener.assert_not_called()

    def test_real_git_checkpoint_reaches_remote_before_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            remote, work = Path(directory) / 'remote.git', Path(directory) / 'work'
            work.mkdir()
            def git(*args, cwd=work):
                result = subprocess.run(['git', *args], cwd=cwd, capture_output=True, check=True)
                return result.stdout.decode()
            git('init', '--bare', str(remote))
            git('init')
            git('checkout', '-b', 'main')
            git('config', 'user.name', 'Amiees local test')
            git('config', 'user.email', 'test@example.invalid')
            git('remote', 'add', 'origin', str(remote))
            (work / 'ig_state.json').write_text('{}')
            git('add', 'ig_state.json')
            git('commit', '-m', 'Initialize isolated test')
            git('push', 'origin', 'HEAD:main')
            def api(method, path, **params):
                if path.endswith('/media_publish'):
                    saved = json.loads(git('--git-dir', str(remote), 'show', 'main:ig_state.json'))
                    self.assertEqual(saved['test-1']['phase'], 'publishing')
                    return {'id': 'public-1'}
                if params.get('fields') == 'permalink':
                    return {'permalink': 'https://www.instagram.com/p/test/'}
                return {'id': 'container-1', 'status_code': 'FINISHED'}
            with patch.object(p, 'HERE', str(work)), patch.object(p, 'STATE', str(work / 'ig_state.json')), patch.object(p, 'DURABLE_GIT', True), patch.object(p, 'call', side_effect=api):
                p.publish('account-1', self.row, self.state)
                p.checkpoint(self.state)  # An unchanged checkpoint must also succeed.
            saved = json.loads(git('--git-dir', str(remote), 'show', 'main:ig_state.json'))
            self.assertEqual(saved['test-1']['phase'], 'published')


if __name__ == '__main__':
    unittest.main()

