from pathlib import Path
from unittest.mock import patch

from config import config
from ok.test.TaskTestCase import TaskTestCase
from src.Labels import Labels
from src.char.BaseChar import CharType, Elements
from src.char.CharFactory import get_char_by_pos
from src.char.Lily import Lily
from src.task.AutoCombatTask import AutoCombatTask


ROOT = Path(__file__).resolve().parents[1]


class TestLily(TaskTestCase):
    task_class = AutoCombatTask
    config = {**config, 'template_matching': dict(config['template_matching'])}

    def lily(self):
        char = Lily(self.task, 0, char_name=Labels.char_lily,
                    ring_index=Elements.SPECTRO, char_type=CharType.HEALER)
        char.is_current_char = True
        self.task.cds = {}
        self.task.use_liberation = True
        self.task.chars = [char, None, None]
        return char

    def test_portrait_registration_and_other_slot_negative(self):
        for image in ('lily-start.png', 'lily-heavy.png', 'lily-slumberland.png', 'lily-validation.png'):
            with self.subTest(image=image):
                self.set_image(str(ROOT / 'ok_templates' / image))
                char = get_char_by_pos(self.task, self.task.get_box_by_name('box_char_1'), 0, None)
                self.assertIsInstance(char, Lily)
                self.assertEqual(char.char_type, CharType.HEALER)
                self.assertEqual(char.ring_index, Elements.SPECTRO)
                self.assertEqual(char.buff_time, 30)
                self.assertIsNone(self.task.find_one(
                    Labels.char_lily, box=self.task.get_box_by_name('box_char_2'), threshold=0.6))

    def test_ready_channel_and_slumberland_are_distinct(self):
        char = self.lily()
        for image, full, channel, slumberland, liberation in (
            ('lily-start.png', False, False, False, True),
            ('lily-ready.png', True, False, False, True),
            ('lily-heavy.png', False, True, False, True),
            ('lily-validation.png', False, True, False, True),
            ('lily-slumberland.png', False, False, True, False),
        ):
            with self.subTest(image=image):
                self.set_image(str(ROOT / 'ok_templates' / image))
                self.task.cds = {}
                self.assertEqual(bool(char.is_mouse_forte_full()), full)
                self.assertEqual(char.heavy_in_progress(), channel)
                self.assertEqual(char.in_slumberland(), slumberland)
                self.assertEqual(char.skip_combat_check(), slumberland)
                self.assertEqual(bool(char.liberation_available()), liberation)

    def test_slumberland_hands_off_without_cancelling_it(self):
        self.set_image(str(ROOT / 'ok_templates/lily-slumberland.png'))
        char = self.lily()
        with (
            patch.object(char, 'click_echo') as echo,
            patch.object(char, 'build_forte') as forte,
            patch.object(char, 'fill_concerto') as concerto,
            patch.object(char, 'click_liberation') as liberation,
            patch.object(self.task, 'is_con_full', return_value=False) as full_check,
            patch.object(char, 'switch_next_char') as switch,
        ):
            char.perform()
        echo.assert_not_called()
        forte.assert_not_called()
        concerto.assert_not_called()
        liberation.assert_not_called()
        full_check.assert_not_called()
        switch.assert_called_once()

    def test_optional_abyss_character_info_from_screenshot(self):
        self.set_image(str(ROOT / 'ok_templates/lily-help.png'))
        self.assertTrue(self.task.find_abyss_character_info())

        self.set_image(str(ROOT / 'ok_templates/lily-start.png'))
        with patch.object(self.task, 'click') as click:
            self.assertFalse(self.task.find_abyss_character_info())
        click.assert_not_called()
