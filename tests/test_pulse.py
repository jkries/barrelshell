from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import barrel_v1 as core
import pulse_tasks as fmt

NOW = datetime(2030, 1, 2, 12, 0)


def section(name='test-task', schedule='@every 30m', prompt='Do the task.', expires=None):
    extra = f'expires: {expires}\n' if expires is not None else ''
    return f'## {name}\nschedule: {schedule}\n{extra}\n{prompt}\n\n'


class FormatTests(unittest.TestCase):
    def test_legacy_tasks_aliases_and_intervals_still_parse(self):
        text = '# Pulse\n\n' + ''.join(section(name, schedule) for name, schedule in (
            ('cron-task', '0 7 * * 1-5'), ('boot-task', '@reboot'),
            ('idle-task', '@idle 4h'), ('interval-task', '@every 30m'), ('alias-task', '@daily')))
        tasks, problems = fmt.parse(text)
        self.assertEqual(len(tasks), 5)
        self.assertEqual(problems, [])
        self.assertTrue(all(t['expires'] is None for t in tasks))

    def test_expiration_timestamp_date_boundary_and_timezone(self):
        self.assertEqual(fmt.parse_time('2030-01-02', end_of_date=True), datetime(2030, 1, 3))
        task = fmt.parse(section(expires='2030-01-02'))[0][0]
        self.assertFalse(fmt.expired(task, datetime(2030, 1, 2, 23, 59)))
        self.assertTrue(fmt.expired(task, datetime(2030, 1, 3)))
        expected = datetime.fromisoformat('2030-01-02T12:00:00+00:00').astimezone().replace(tzinfo=None)
        self.assertEqual(fmt.parse_time('2030-01-02T12:00Z'), expected)

    def test_once_requires_valid_time(self):
        self.assertEqual(fmt.schedule_kind('@once 2030-01-02 12:00'), ('once', NOW))
        for schedule in ('@once 2030-01-02', '@once tomorrow', '@every 0m', '@every ' + '9' * 100 + 'd',
                         '0 7 * * *\n## injected'):
            with self.subTest(schedule=schedule):
                self.assertIsNone(fmt.schedule_kind(schedule)[0])

    def test_invalid_expiry_duplicate_names_and_empty_prompts_are_disabled(self):
        for text in (section(expires='someday'), section(expires='2030-02-30'),
                     section(prompt=''), section('same') + section('SAME'),
                     section('_last_user_contact'),
                     section(schedule='@once 2030-01-02 12:00', expires='2030-01-02 11:00')):
            with self.subTest(text=text):
                tasks, problems = fmt.parse(text)
                self.assertEqual(tasks, [])
                self.assertTrue(problems)

    def test_exact_removal_preserves_unrelated_content(self):
        prefix = '# Pulse\n\nPersonal notes before tasks.\n\n'
        text = prefix + section('brief') + section('brief-extra') + section('other')
        result, removed = fmt.remove_sections(text, ['BRIEF'])
        self.assertEqual(removed, ['brief'])
        self.assertEqual(result, prefix + section('brief-extra') + section('other'))
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            fmt.remove_sections(section('same') + section('Same'), ['same'])

    def test_prompt_text_is_not_interpreted_as_expiry(self):
        tasks, _ = fmt.parse(section(prompt='This event expired in 2001. Keep checking anyway.'))
        self.assertFalse(fmt.expired(tasks[0], NOW))
        tasks, _ = fmt.parse(section(prompt='expires: 2000-01-01\nThis is quoted prompt text, not metadata.'))
        self.assertIsNone(tasks[0]['expires'])
        self.assertIn('expires: 2000', tasks[0]['prompt'])

    def test_failed_atomic_replace_preserves_file_and_removes_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'pulse.md'
            path.write_text('original')
            with patch.object(fmt.os, 'replace', side_effect=OSError('file locked')):
                with self.assertRaises(OSError):
                    fmt.atomic_write(path, 'replacement')
            self.assertEqual(path.read_text(), 'original')
            self.assertEqual(list(Path(directory).glob('*.tmp')), [])

    def test_shipped_example_still_parses_without_warnings(self):
        path = Path(core.__file__).parent / 'pulse.example.md'
        tasks, problems = fmt.parse(path.read_text(encoding='utf-8'))
        self.assertEqual(len(tasks), 7)
        self.assertEqual(problems, [])


class PulseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.pulse = root / 'pulse.md'
        self.state = root / 'pulse_state.json'
        self.pending = root / 'pulse_pending.json'
        self.patches = [patch.object(core, 'PULSE_FILE', str(self.pulse)),
                        patch.object(core, 'PULSE_STATE_FILE', str(self.state)),
                        patch.object(core, 'PENDING_PULSE_FILE', str(self.pending)),
                        patch.object(core, 'PULSE_CHAT_ID', 0),
                        patch.object(core, 'agent_lock', threading.RLock()),
                        patch.object(core, 'last_contact_at', NOW),
                        patch.object(core, '_pulse_running', {}),
                        patch.object(core, 'pulse_problems', []),
                        patch.object(core, '_pulse_warned', set()),
                        patch.object(core, 'log_event'), patch.object(core, 'deliver'),
                        patch.object(core, 'handle_turn', return_value='Task completed.')]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def write(self, text, state=None, pending=None):
        self.pulse.write_text(text, encoding='utf-8')
        if state is not None:
            fmt.save_json(self.state, state)
        if pending is not None:
            fmt.save_json(self.pending, pending)

    def read_state(self):
        return json.loads(self.state.read_text()) if self.state.exists() else {}

    def test_commands_remove_active_exactly_and_clear_state(self):
        self.write(section('brief') + section('brief-extra'),
                   {'brief': NOW.isoformat(), 'brief-extra': NOW.isoformat(), fmt.LAST_CONTACT_KEY: NOW.isoformat()})
        self.assertIn('brief-extra', core.handle_command('/pulse list'))
        result = core.handle_command('/pulse remove BRIEF')
        self.assertIn('Removed', result)
        self.assertEqual([t['name'] for t in core.parse_pulse()], ['brief-extra'])
        self.assertNotIn('brief', self.read_state())
        self.assertIn(fmt.LAST_CONTACT_KEY, self.read_state())
        self.assertIn('Removed', core.handle_command('/pulse_remove brief-extra'))
        self.assertIn('Use /pulse remove', core.handle_command('/pulse remove'))

    def test_remove_pending_or_malformed_section_and_refuse_duplicates(self):
        self.write(section('broken', 'invalid'), pending={'pending-task': {'cron': '@every 1h', 'prompt': 'Hi'}})
        self.assertIn('Removed', core.remove_pulse_task('pending-task'))
        self.assertEqual(json.loads(self.pending.read_text()), {})
        self.assertIn('Removed', core.remove_pulse_task('broken'))
        duplicate = section('same') + section('Same')
        self.write(duplicate)
        self.assertIn('Not removed', core.remove_pulse_task('same'))
        self.assertEqual(self.pulse.read_text(), duplicate)
        self.assertIn('No task named', core.remove_pulse_task('not-here'))

    def test_expired_tasks_are_removed_before_they_can_fire(self):
        self.write('# Pulse\n\n' + section('expired-task', '@once 2029-12-31 12:00', expires='2030-01-01') + section('permanent'),
                   {'expired-task': '2029-01-01T00:00:00', 'permanent': NOW.isoformat()})
        core.pulse_tick(NOW)
        self.assertEqual([t['name'] for t in core.parse_pulse()], ['permanent'])
        self.assertNotIn('expired-task', self.read_state())
        core.handle_turn.assert_not_called()

    def test_manual_cleanup_and_pending_expiration(self):
        self.write(section('old-task', expires='2000-01-01') + section('permanent'), pending={
            'old-proposal': {'cron': '@every 1h', 'prompt': 'x', 'expires': '2000-01-01'},
            'missed-once': {'cron': '@once 2000-01-01 12:00', 'prompt': 'x'},
            'keep-proposal': {'cron': '@every 1h', 'prompt': 'x'}})
        result = core.handle_command('/pulse cleanup')
        self.assertIn('old-task', result)
        self.assertIn('old-proposal (pending)', result)
        self.assertIn('missed-once (pending)', result)
        self.assertEqual(list(json.loads(self.pending.read_text())), ['keep-proposal'])
        self.assertEqual([t['name'] for t in core.parse_pulse()], ['permanent'])

    def test_once_runs_once_and_removes_section_and_state(self):
        self.write(section('one-off', '@once 2030-01-02 11:00') + section('keep-task'), {'keep-task': NOW.isoformat()})
        core.pulse_tick(NOW)
        self.assertEqual(core.handle_turn.call_count, 1)
        self.assertEqual(core.handle_turn.call_args.args[0], 0)  # local-only delivery
        self.assertEqual([t['name'] for t in core.parse_pulse()], ['keep-task'])
        self.assertNotIn('one-off', self.read_state())
        self.assertNotIn(fmt.COMPLETED_KEY, self.read_state())
        core.pulse_tick(NOW + timedelta(seconds=30))
        self.assertEqual(core.handle_turn.call_count, 1)

    def test_once_failure_is_retained_for_retry_then_removed(self):
        self.write(section('one-off', '@once 2030-01-02 11:00'))
        core.handle_turn.side_effect = [RuntimeError('model offline'), 'Done']
        core.pulse_tick(NOW)
        self.assertEqual(len(core.parse_pulse()), 1)
        self.assertNotIn(fmt.COMPLETED_KEY, self.read_state())
        core.pulse_tick(NOW + timedelta(seconds=30))
        self.assertEqual(core.parse_pulse(), [])
        self.assertEqual(core.handle_turn.call_count, 2)

    def test_delivery_failure_does_not_discard_once_task(self):
        self.write(section('one-off', '@once 2030-01-02 11:00'))
        core.deliver.side_effect = RuntimeError('Telegram offline')
        core.pulse_tick(NOW)
        self.assertEqual(len(core.parse_pulse()), 1)
        self.assertNotIn(fmt.COMPLETED_KEY, self.read_state())

    def test_silent_once_is_successful(self):
        self.write(section('one-off', '@once 2030-01-02 11:00'))
        core.handle_turn.return_value = '**PULSE_OK**'
        core.pulse_tick(NOW)
        core.deliver.assert_not_called()
        self.assertEqual(core.parse_pulse(), [])

    def test_completion_marker_prevents_repeat_when_file_cleanup_fails(self):
        self.write(section('one-off', '@once 2030-01-02 11:00'))
        original = fmt.atomic_write

        def fail_task_file(path, text):
            if Path(path) == self.pulse:
                raise OSError('file locked by editor')
            return original(path, text)

        with patch.object(fmt, 'atomic_write', side_effect=fail_task_file):
            with self.assertRaises(OSError):
                core.pulse_tick(NOW)
        self.assertIn('one-off', self.read_state()[fmt.COMPLETED_KEY])
        core.pulse_tick(NOW + timedelta(seconds=30))
        self.assertEqual(core.handle_turn.call_count, 1)
        self.assertEqual(core.parse_pulse(), [])

    def test_future_once_and_new_intervals_do_not_fire_early(self):
        self.write(section('future-once', '@once 2030-01-02 13:00') + section('interval-task', '@every 30m'))
        core.pulse_tick(NOW)
        core.handle_turn.assert_not_called()
        core.pulse_tick(NOW + timedelta(minutes=29))
        core.handle_turn.assert_not_called()
        core.pulse_tick(NOW + timedelta(minutes=30))
        self.assertEqual(core.handle_turn.call_count, 1)
        self.assertIn('interval-task', self.pulse.read_text())

    def test_idle_first_period_and_repeat_suppression(self):
        self.write(section('idle-task', '@idle 1h'))
        core.pulse_tick(NOW + timedelta(minutes=30))
        core.handle_turn.assert_not_called()
        self.assertNotIn('idle-task', self.read_state())
        core.pulse_tick(NOW + timedelta(hours=1))
        core.pulse_tick(NOW + timedelta(hours=2))
        self.assertEqual(core.handle_turn.call_count, 1)
        core.last_contact_at = NOW + timedelta(hours=2, minutes=1)
        core.pulse_tick(NOW + timedelta(hours=3, minutes=1))
        self.assertEqual(core.handle_turn.call_count, 2)

    def test_reboot_guard_and_corrupt_last_run_do_not_break_tick(self):
        self.write(section('boot-task', '@reboot') + section('interval-task', '@every 1h'),
                   {'interval-task': 'bad-state'})
        core.pulse_tick(NOW, startup=True)
        core.pulse_tick(NOW + timedelta(minutes=1), startup=True)
        self.assertEqual(core.handle_turn.call_count, 1)
        core.pulse_tick(NOW)
        self.assertEqual(self.read_state()['interval-task'], NOW.isoformat())
        core.pulse_tick(NOW + timedelta(minutes=11), startup=True)
        self.assertEqual(core.handle_turn.call_count, 2)

    def test_warnings_clear_after_bad_metadata_is_fixed(self):
        self.write(section(expires='bad date'))
        self.assertEqual(core.parse_pulse(), [])
        self.assertTrue(core.pulse_problems)
        self.write(section())
        self.assertEqual(len(core.parse_pulse()), 1)
        self.assertEqual(core.pulse_problems, [])

    def test_completion_markers_apply_only_to_the_same_task_definition(self):
        original = fmt.parse(section('one-off', '@once 2030-01-02 11:00'))[0][0]
        self.write(section('one-off', '@once 2030-01-02 11:00', prompt='A different explicitly edited task.'),
                   {fmt.COMPLETED_KEY: {'one-off': fmt.fingerprint(original)}})
        self.assertEqual(core.cleanup_expired_pulse(NOW), [])
        self.assertEqual(len(core.parse_pulse()), 1)

    def test_model_cannot_remove_or_cleanup(self):
        original = section('keep-task', expires='2000-01-01')
        self.write(original)
        for command in ('remove keep-task', 'cleanup'):
            self.assertIn('only the human', core.run_pulse(command, 0))
        self.assertEqual(self.pulse.read_text(), original)

    def test_proposal_expiry_is_approved_explicitly_and_pipe_prompt_is_preserved(self):
        result = core.run_pulse('add temp-task | @every 1h | Read <file>read x | y</file> | expires: 2037-01-01', 0)
        self.assertIn('NOT active', result)
        self.assertFalse(self.pulse.exists())
        pending = json.loads(self.pending.read_text())
        self.assertEqual(pending['temp-task']['expires'], '2037-01-02T00:00:00')
        self.assertIn('x | y', pending['temp-task']['prompt'])
        self.assertIn('Approved', core.approve_pending('temp-task'))
        task = core.parse_pulse()[0]
        self.assertEqual(task['expires'], '2037-01-02T00:00:00')
        self.assertEqual(task['prompt'], 'Read <file>read x | y</file>')

    def test_unicode_proposal_survives_persistence_and_approval(self):
        prompt = 'Read the caf\u00e9 announcement: \u4e2d\u6587'
        self.assertIn('NOT active', core.run_pulse('add unicode-task | @every 1h | ' + prompt, 0))
        self.assertIn('Approved', core.approve_pending('unicode-task'))
        self.assertEqual(core.parse_pulse()[0]['prompt'], prompt)

    def test_bad_or_expired_proposals_cannot_be_approved(self):
        self.write('', pending={'old-task': {'cron': '@once 2000-01-01 12:00', 'prompt': 'x'}})
        self.assertIn('Not approved', core.approve_pending('old-task'))
        self.assertEqual(self.pulse.read_text(), '')
        self.assertIn('refused', core.run_pulse('add injected | @every 1h | Hi\n## second-task\nschedule: @every 1m\nBad', 0))
        self.assertIn('refused', core.run_pulse('add old-expiry | @every 1h | Hi | expires: 2000-01-01', 0))
        self.assertIn('refused', core.run_pulse('add impossible | @once 2037-01-02 12:00 | Hi | expires: 2037-01-01', 0))

    def test_approved_reboot_and_idle_are_not_marked_as_already_fired(self):
        self.write('', pending={'boot-task': {'cron': '@reboot', 'prompt': 'x'},
                                'idle-task': {'cron': '@idle 1h', 'prompt': 'x'}})
        core.approve_pending('boot-task')
        core.approve_pending('idle-task')
        self.assertNotIn('boot-task', self.read_state())
        self.assertNotIn('idle-task', self.read_state())

    def test_local_only_loop_runs_instead_of_disabling_chat_zero(self):
        with patch.object(core, 'pulse_tick') as tick, patch.object(core, 'fire_due_reminders') as reminders, \
             patch.object(core.time, 'sleep', side_effect=InterruptedError('stop test loop')):
            with self.assertRaises(InterruptedError):
                core.pulse_loop()
        self.assertEqual(tick.call_count, 2)
        tick.assert_any_call(startup=True)
        reminders.assert_called_once()

    def test_task_removed_while_queued_never_starts(self):
        self.write(section('one-off', '@once 2030-01-02 11:00'))
        task = core.parse_pulse()[0]
        entered = threading.Event()
        result = []

        def queued():
            entered.set()
            result.append(core.fire_task(task, now=NOW))

        with core.agent_lock:
            thread = threading.Thread(target=queued)
            thread.start()
            self.assertTrue(entered.wait(2))
            core.remove_pulse_task('one-off')
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [False])
        core.handle_turn.assert_not_called()
        self.assertNotIn('one-off', self.read_state())

    def test_removing_running_task_does_not_resurrect_state(self):
        self.write(section('one-off', '@once 2030-01-02 11:00'))
        started, finish = threading.Event(), threading.Event()
        errors = []

        def run_model(*args, **kwargs):
            started.set()
            if not finish.wait(5):
                raise RuntimeError('test did not release model')
            return 'Done'

        def run_task():
            try:
                core.fire_task(core.parse_pulse()[0], now=NOW)
            except Exception as exc:
                errors.append(exc)

        core.handle_turn.side_effect = run_model
        thread = threading.Thread(target=run_task)
        thread.start()
        try:
            self.assertTrue(started.wait(2))
            self.assertIn('already running', core.remove_pulse_task('one-off'))
        finally:
            finish.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(core.parse_pulse(), [])
        self.assertNotIn('one-off', self.read_state())
        self.assertNotIn(fmt.COMPLETED_KEY, self.read_state())


if __name__ == '__main__':
    unittest.main()
