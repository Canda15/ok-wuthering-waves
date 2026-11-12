import unittest
import re
from pathlib import Path
from unittest.mock import Mock, patch
from types import SimpleNamespace

import cv2
import numpy as np

from ok.device.interaction_methods.foreground_input import ForegroundInputError, ForegroundInputInterrupted

from src.task.TacetEliminationTask import TacetEliminationTask, read_board, matches, choose_move


class TestTacetElimination(unittest.TestCase):
    def test_live_board_at_supported_resolutions(self):
        image = cv2.imread(str(Path(__file__).parent / 'images/tacet_elimination_board.jpg'))
        expected = ['PWBPRRPB', 'PGPPRGBP', 'GGROWBBW', 'PBBWGOWW',
                    'GRPOPBBP', 'RPGBWBGG', 'GGBPWGRB']
        for size in ((1280, 720), (1920, 1080), (3840, 2160)):
            with self.subTest(size=size):
                board = read_board(cv2.resize(image, size))
                self.assertEqual([''.join(row) for row in board], expected)
                move = choose_move(board)
                self.assertIsNotNone(move)
                _, a, b = move
                board[a[0]][a[1]], board[b[0]][b[1]] = board[b[0]][b[1]], board[a[0]][a[1]]
                self.assertTrue(any(a in group or b in group for group in matches(board)))

    def test_blank_capture_produces_no_moves(self):
        board = read_board(np.zeros((720, 1280, 3), dtype=np.uint8))
        self.assertTrue(all(cell == '?' for row in board for cell in row))
        self.assertIsNone(choose_move(board))

    def test_final_stage_board(self):
        image = cv2.imread(str(Path(__file__).parent / 'images/tacet_elimination_final.jpg'))
        expected = ['BRGBGRGP', 'WGBPGPHG', 'BRWPRRWB', 'GGWBBRPI',
                    'IOPRWWOI', 'IIHIIHII', 'OIIIIIIO']
        for size in ((1280, 720), (1600, 900), (1920, 1080)):
            with self.subTest(size=size):
                board = read_board(cv2.resize(image, size))
                self.assertEqual([''.join(row) for row in board], expected)
                self.assertIsNotNone(choose_move(board))

    def stage_sweep_task(self, pages):
        task = SimpleNamespace(tr=str, width=1600, height=900, text=TacetEliminationTask.text)
        for name in ('next_frame', 'drag_relative', 'sleep', 'log_info', 'click'):
            setattr(task, name, Mock())
        task.ocr = Mock(side_effect=pages)
        return task

    def test_final_stage_with_record_is_opened_after_prerequisite_sweep(self):
        early = [SimpleNamespace(name='已完成'), SimpleNamespace(name='已完成')]
        final = [SimpleNamespace(name='终局消除', x=1200), SimpleNamespace(name='最高纪录：4960'),
                 SimpleNamespace(name='已完成')]
        task = self.stage_sweep_task([early, final, final])
        self.assertTrue(TacetEliminationTask.open_unfinished_stage(task))
        task.click.assert_called_once_with(final[0], after_sleep=.7)
        self.assertTrue(task.final_stage)

    def test_final_stage_without_record_still_requires_prerequisites(self):
        final = [SimpleNamespace(name='终局消除', x=1200), SimpleNamespace(name='暂无记录')]
        task = self.stage_sweep_task([final, final])
        with self.assertRaisesRegex(RuntimeError, 'completion status'):
            TacetEliminationTask.open_unfinished_stage(task)
        task.click.assert_not_called()

    def test_unfinished_prerequisite_has_priority_over_final_stage(self):
        unfinished = SimpleNamespace(name='未完成', x=700, y=690, width=100, height=30)
        task = self.stage_sweep_task([[SimpleNamespace(name='终局消除', x=1200), unfinished]])
        self.assertTrue(TacetEliminationTask.open_unfinished_stage(task))
        self.assertFalse(task.final_stage)
        task.click.assert_called_once_with(.46875, .5833333333333333, after_sleep=.7)

    def test_final_card_position_tolerates_other_ocr_changes(self):
        early = [SimpleNamespace(name='已完成'), SimpleNamespace(name='已完成')]
        first = [SimpleNamespace(name='终局消除', x=1200), SimpleNamespace(name='最高纪录：4960')]
        second = [SimpleNamespace(name='终局消除', x=1204), SimpleNamespace(name='最高记录：4960')]
        task = self.stage_sweep_task([early, first, second])
        self.assertTrue(TacetEliminationTask.open_unfinished_stage(task))
        task.click.assert_called_once_with(second[0], after_sleep=.7)

    def test_locked_prerequisite_blocks_final_stage(self):
        task = self.stage_sweep_task([[SimpleNamespace(name='1天后解锁')]])
        with self.assertRaisesRegex(RuntimeError, 'still locked'):
            TacetEliminationTask.open_unfinished_stage(task)
        task.click.assert_not_called()

    def test_board_continues_past_twenty_and_one_hundred_twenty_moves(self):
        task = SimpleNamespace(tr=str, frame=None)
        for name in ('next_frame', 'wait_board_settled', 'screenshot', 'sleep',
                     'info_set', 'log_info', 'swap', 'send_key'):
            setattr(task, name, Mock())
        task.dismiss_event_help = Mock(return_value=False)
        task.is_board = Mock(side_effect=[True] * 125 + [False])
        boards = [[list('RGBPW' + str(index))] for index in range(125)]
        with patch('src.task.TacetEliminationTask.read_board', side_effect=boards), patch(
                'src.task.TacetEliminationTask.choose_move', return_value=(10, (0, 0), (0, 1))):
            TacetEliminationTask.play_board(task)
        self.assertEqual(task.swap.call_count, 125)
        task.send_key.assert_not_called()

    def board_task(self, iterations):
        task = SimpleNamespace(tr=str, frame=None)
        for name in ('next_frame', 'wait_foreground_frame', 'wait_board_settled', 'screenshot',
                     'sleep', 'info_set', 'log_info', 'swap'):
            setattr(task, name, Mock())
        task.dismiss_event_help = Mock(return_value=False)
        task.is_board = Mock(side_effect=[True] * iterations + [False])
        return task

    def test_interrupted_drag_recomputes_move_from_new_board_without_old_exclusions(self):
        task = self.board_task(2)
        task.swap.side_effect = [ForegroundInputInterrupted(), None]
        boards = [[list('RGB')], [list('BGR')]]
        moves = [(10, (0, 0), (0, 1)), (20, (0, 1), (0, 2))]
        observed = []

        def choose(board, excluded):
            observed.append((board, frozenset(excluded)))
            return moves[len(observed) - 1]

        with patch('src.task.TacetEliminationTask.read_board', side_effect=boards), \
                patch('src.task.TacetEliminationTask.choose_move', side_effect=choose):
            TacetEliminationTask.play_board(task)
        self.assertEqual(observed, [(boards[0], frozenset()), (boards[1], frozenset())])
        self.assertEqual([call.args for call in task.swap.call_args_list],
                         [moves[0][1:], moves[1][1:]])
        task.info_set.assert_called_once_with('Move', 1)
        task.wait_foreground_frame.assert_called_once()

    def test_repeated_focus_interruptions_do_not_consume_board_stall_budget(self):
        task = self.board_task(10)
        task.swap.side_effect = [ForegroundInputInterrupted()] * 9 + [None]
        with patch('src.task.TacetEliminationTask.read_board', return_value=[list('RGB')]), \
                patch('src.task.TacetEliminationTask.choose_move', return_value=(10, (0, 0), (0, 1))):
            TacetEliminationTask.play_board(task)
        self.assertEqual(task.swap.call_count, 10)
        task.info_set.assert_called_once_with('Move', 1)
        task.screenshot.assert_not_called()

    def test_board_system_input_failure_is_not_retried(self):
        task = self.board_task(1)
        task.swap.side_effect = ForegroundInputError()
        with patch('src.task.TacetEliminationTask.read_board', return_value=[list('RGB')]), \
                patch('src.task.TacetEliminationTask.choose_move', return_value=(10, (0, 0), (0, 1))), \
                self.assertRaises(ForegroundInputError):
            TacetEliminationTask.play_board(task)
        task.swap.assert_called_once()
        task.wait_foreground_frame.assert_not_called()

    def test_interrupted_result_exit_preserves_completion_and_never_replays_final(self):
        for accepted in (False, True):
            with self.subTest(accepted=accepted):
                success = [SimpleNamespace(name='挑战成功'), SimpleNamespace(name='退出关卡')]
                completed = [SimpleNamespace(name='已完成')]
                state = SimpleNamespace(page=success, exits=0)
                task = SimpleNamespace(tr=str, text=TacetEliminationTask.text,
                    is_board=Mock(side_effect=[True, True] + [False] * 4),
                    ocr=Mock(return_value=[SimpleNamespace(name='最高纪录：4960')]),
                    event_text=lambda: state.page,
                    wait_until=lambda condition, **kwargs: condition(),
                    open_unfinished_stage=Mock(),
                )
                for name in ('info_set', 'next_frame', 'wait_foreground_frame', 'log_info', 'screenshot'):
                    setattr(task, name, Mock())

                def click_text(boxes, pattern):
                    if any(re.search(pattern, box.name, re.I) for box in boxes):
                        state.exits += 1
                        if state.exits == 1:
                            if accepted:
                                state.page = completed
                            raise ForegroundInputInterrupted()
                        state.page = completed
                        return True
                    return False

                task.click_text = click_text
                TacetEliminationTask.run_event(task)
                self.assertEqual(task.completed_stages, 1)
                self.assertEqual(state.exits, 1 if accepted else 2)
                task.open_unfinished_stage.assert_not_called()

    def test_success_page_exits_instead_of_replaying(self):
        success = [SimpleNamespace(name=name) for name in ('挑战成功', '退出关卡', '再次挑战')]
        completed = [SimpleNamespace(name='已完成')]
        task = SimpleNamespace(tr=str, text=TacetEliminationTask.text)
        for name in ('prepare_event', 'info_set', 'next_frame', 'log_info', 'screenshot'):
            setattr(task, name, Mock())
        task.is_board = Mock(side_effect=[True, True, False, False])
        task.ocr = Mock(return_value=[SimpleNamespace(name='最高纪录：4960')])
        task.event_text = Mock(side_effect=[success, success, completed])
        task.open_unfinished_stage = Mock(return_value=False)
        clicked = []

        def click_text(boxes, pattern):
            for box in boxes:
                if re.search(pattern, box.name, re.I):
                    clicked.append(box.name)
                    return True
            return False

        task.click_text = click_text
        task.wait_until = lambda predicate, **kwargs: predicate()
        TacetEliminationTask.run_event(task)
        self.assertEqual(clicked, ['退出关卡'])
        self.assertEqual(task.completed_stages, 1)

    def test_finished_final_detail_does_not_start_another_round(self):
        success = [SimpleNamespace(name=name) for name in ('挑战成功', '退出关卡', '再次挑战')]
        details = [SimpleNamespace(name='终局消除'), SimpleNamespace(name='开始挑战')]
        task = SimpleNamespace(tr=str, text=TacetEliminationTask.text, return_to_stage_list=Mock())
        for name in ('info_set', 'next_frame', 'log_info', 'screenshot'):
            setattr(task, name, Mock())
        task.is_board = Mock(side_effect=[True, True, False, False])
        task.ocr = Mock(return_value=[SimpleNamespace(name='最高纪录：4960')])
        task.event_text = Mock(side_effect=[success, success, details])
        clicked = []

        def click_text(boxes, pattern):
            for box in boxes:
                if re.search(pattern, box.name, re.I):
                    clicked.append(box.name)
                    return True
            return False

        task.click_text = click_text
        task.wait_until = lambda predicate, **kwargs: predicate()
        TacetEliminationTask.run_event(task)
        self.assertEqual(clicked, ['退出关卡'])
        task.return_to_stage_list.assert_called_once()

    def test_return_to_stage_list_retries_only_while_details_are_visible(self):
        task = SimpleNamespace(tr=str, send_key=Mock(),
                               ocr=Mock(side_effect=[[SimpleNamespace(name='开始挑战')], []]),
                               wait_until=lambda predicate, **kwargs: predicate())
        TacetEliminationTask.return_to_stage_list(task)
        self.assertEqual(task.send_key.call_count, 2)
        task.send_key.assert_called_with('esc', down_time=.1, after_sleep=.7)

    def test_resumed_final_board_can_finish_when_initial_record_ocr_is_empty(self):
        success = [SimpleNamespace(name=name) for name in ('挑战成功', '退出关卡', '再次挑战')]
        details = [SimpleNamespace(name='终局消除'), SimpleNamespace(name='开始挑战')]
        task = SimpleNamespace(tr=str, text=TacetEliminationTask.text, return_to_stage_list=Mock())
        for name in ('info_set', 'next_frame', 'log_info', 'screenshot', 'play_board', 'sleep'):
            setattr(task, name, Mock())
        task.is_board = Mock(side_effect=[True, True, True, False, False, False])
        task.ocr = Mock(return_value=[])
        task.event_text = Mock(side_effect=[success, success, details, [SimpleNamespace(name='已完成')]])
        task.open_unfinished_stage = Mock(side_effect=AssertionError('Would start another round'))
        task.wait_until = lambda predicate, **kwargs: predicate()
        task.click_text = lambda boxes, pattern: any(re.search(pattern, box.name, re.I) for box in boxes)
        TacetEliminationTask.run_event(task)
        task.return_to_stage_list.assert_called_once()
        task.open_unfinished_stage.assert_not_called()

    def test_bottles_do_not_match_or_swap(self):
        board = [['O'] * 8 for _ in range(7)]
        self.assertEqual(matches(board), [])
        self.assertIsNone(choose_move(board))

    def test_ice_is_distinct_from_blue_echoes(self):
        image = cv2.imread(str(Path(__file__).parent / 'images/tacet_elimination_ice.jpg'))
        board = read_board(image)
        self.assertEqual(''.join(board[0]), 'IIIGWIII')
        self.assertEqual(board[1][3], 'B')
        move = choose_move(board)
        self.assertIsNotNone(move)
        for row, col in move[1:]:
            self.assertNotEqual(board[row][col], 'I')

    def test_two_rainbow_pieces_clear_whole_board(self):
        board = [['I'] * 8 for _ in range(7)]
        board[2][2] = board[2][3] = '*'
        move = choose_move(board)
        self.assertEqual(move[1:], ((2, 2), (2, 3)))
        self.assertGreaterEqual(move[0], 56)

    def test_hourglass_recognition_and_delivery_move(self):
        image = cv2.imread(str(Path(__file__).parent / 'images/tacet_elimination_hourglass.jpg'))
        board = read_board(image)
        self.assertEqual(board[2][7], 'H')
        self.assertEqual(board[3][5], 'H')
        self.assertEqual(board[5][3], 'H')
        self.assertNotIn('?', ''.join(''.join(row) for row in board))
        self.assertIsNotNone(choose_move(board))

    def test_blank_stage_list_cannot_report_completion(self):
        task = TacetEliminationTask.__new__(TacetEliminationTask)
        task.tr = str
        for name in ('next_frame', 'drag_relative', 'sleep', 'log_info', 'click'):
            setattr(task, name, Mock())
        task.ocr = Mock(return_value=[])
        with self.assertRaisesRegex(RuntimeError, 'completion status'):
            task.open_unfinished_stage()
        task.click.assert_not_called()

    def test_wrong_start_screen_does_not_click(self):
        task = TacetEliminationTask.__new__(TacetEliminationTask)
        task.tr = str
        task.next_frame = Mock()
        task.ocr = Mock(return_value=[])
        task.click = Mock()
        with self.assertRaisesRegex(RuntimeError, 'event screen'):
            task.prepare_event()
        task.click.assert_not_called()


if __name__ == '__main__':
    unittest.main()
