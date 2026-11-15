import tempfile
import unittest
import zipfile
import json
from pathlib import Path
from unittest.mock import patch

from ok.util.config import Config
from src.Labels import Labels
from src.char.Baizhi import Baizhi
from src.char.Chixia import Chixia
from src.char.CharFactory import apply_team_char_classes
from src.char.CustomCharLoader import (
    TEAM_CODE_MODE_BUILTIN, TEAM_CODE_MODE_IMPORT, TEAM_CODE_STATE_NONE, changed_import_members,
    clear_custom_char_cache, clear_team_char_cache, create_custom_team, delete_custom_team,
    drifted_team_members, export_custom_team,
    get_custom_char_file, get_custom_char_modes_file,
    get_team_char_file, get_team_code_mode, get_team_import_file,
    import_custom_team, inspect_team_archive, list_custom_teams, load_custom_char_class, load_custom_char_modes,
    normalize_custom_teams, normalize_team_code,
    read_builtin_char_code, read_team_char_code, read_team_import_code, read_team_manifest,
    remove_custom_char_code, save_custom_char_code, save_custom_char_modes, save_team_char_code, save_team_code_as_import,
    save_team_import_code, set_custom_char_enabled, set_team_code_mode, switch_all_teams_code_mode,
    switch_team_code_mode, team_code_state, write_team_manifest,
)
from src.char.Mortefi import Mortefi
from src.char.Verina import Verina


class TestCustomCharLoader(unittest.TestCase):
    def setUp(self):
        self.old_config_folder = Config.config_folder
        self.temp_dir = tempfile.TemporaryDirectory()
        Config.config_folder = self.temp_dir.name
        clear_custom_char_cache()
        clear_team_char_cache()

    def tearDown(self):
        clear_custom_char_cache()
        clear_team_char_cache()
        Config.config_folder = self.old_config_folder
        self.temp_dir.cleanup()

    def test_loads_enabled_custom_char_class(self):
        code = """
from src.char.Mortefi import Mortefi as BuiltinMortefi


class Mortefi(BuiltinMortefi):
    custom_marker = True
"""
        save_custom_char_code(Mortefi, code, use_custom=True)

        custom_cls = load_custom_char_class(Mortefi)

        self.assertIsNot(custom_cls, Mortefi)
        self.assertTrue(custom_cls.custom_marker)
        self.assertTrue(issubclass(custom_cls, Mortefi))

    def test_disabled_custom_char_uses_builtin_class(self):
        code = """
from src.char.Mortefi import Mortefi as BuiltinMortefi


class Mortefi(BuiltinMortefi):
    custom_marker = True
"""
        save_custom_char_code(Mortefi, code, use_custom=True)
        set_custom_char_enabled(Mortefi, False)

        self.assertIs(load_custom_char_class(Mortefi), Mortefi)

    def test_invalid_custom_char_modes_fail_before_changing_code_or_defaults(self):
        path = get_custom_char_modes_file()
        code = get_custom_char_file(Mortefi)
        original_code = read_builtin_char_code(Mortefi).encode('utf-8')
        code.write_bytes(original_code)
        for original in (b'{\r\n"unfinished":', b'[]', b'{"Mortefi": {"use_custom": NaN}}'):
            with self.subTest(original=original):
                path.write_bytes(original)
                with self.assertRaises(ValueError):
                    load_custom_char_class(Mortefi)
                with self.assertRaises(ValueError):
                    set_custom_char_enabled(Chixia, True)
                with self.assertRaises(ValueError):
                    remove_custom_char_code(Mortefi)
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(code.read_bytes(), original_code)

    def test_custom_char_modes_keep_bytes_on_io_failure_and_preserve_unknown_values(self):
        path = get_custom_char_modes_file()
        modes = {'Mortefi': {'use_custom': False, 'extension': {'count': 0, 'value': None}},
                 'Chixia': {'use_custom': True}}
        original = json.dumps(modes, ensure_ascii=False).encode('utf-8')
        path.write_bytes(original)
        with patch('builtins.open', side_effect=PermissionError('合成读取拒绝')), \
                patch('io.open', side_effect=PermissionError('合成读取拒绝')):
            with self.assertRaises(PermissionError):
                load_custom_char_modes()
        with patch('os.replace', side_effect=PermissionError('合成原子提交失败')):
            with self.assertRaises(PermissionError):
                set_custom_char_enabled(Mortefi, True)
        self.assertEqual(path.read_bytes(), original)
        with self.assertRaises(ValueError):
            save_custom_char_modes({'Mortefi': {'use_custom': float('nan')}})
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.iterdir()), [path])
        code_path = get_custom_char_file(Mortefi)
        original_code = self._team_code('原有源码').encode('utf-8').replace(b'\n', b'\r\n', 1)
        code_path.write_bytes(original_code)
        with patch('os.replace', side_effect=PermissionError('合成原子提交失败')):
            with self.assertRaises(PermissionError):
                save_custom_char_code(Mortefi, self._team_code('待保存源码'))
        self.assertEqual(code_path.read_bytes(), original_code)
        self.assertEqual(path.read_bytes(), original)
        set_custom_char_enabled(Mortefi, True)
        modes['Mortefi']['use_custom'] = True
        self.assertEqual(load_custom_char_modes(), modes)

    def test_remove_custom_char_code_deletes_file_and_uses_builtin_class(self):
        code = """
from src.char.Mortefi import Mortefi as BuiltinMortefi


class Mortefi(BuiltinMortefi):
    custom_marker = True
"""
        save_custom_char_code(Mortefi, code, use_custom=True)

        remove_custom_char_code(Mortefi)

        self.assertFalse(get_custom_char_file(Mortefi).exists())
        self.assertIs(load_custom_char_class(Mortefi), Mortefi)

    def test_failed_save_restores_previous_custom_code(self):
        code = """
from src.char.Mortefi import Mortefi as BuiltinMortefi


class Mortefi(BuiltinMortefi):
    custom_marker = True
"""
        save_custom_char_code(Mortefi, code, use_custom=True)
        code_path = get_custom_char_file(Mortefi)
        modes_path = get_custom_char_modes_file()
        # 源码和模式的 BOM、混合换行及未知字段在失败回滚后都应保持原字节。
        original_code = b'\xef\xbb\xbf' + code.encode('utf-8').replace(b'\n', b'\r\n', 1)
        original_modes = b'\xef\xbb\xbf{"Mortefi":{"use_custom":true,"extension":null}}\r\n'
        code_path.write_bytes(original_code)
        modes_path.write_bytes(original_modes)

        with self.assertRaises(RuntimeError):
            save_custom_char_code(Mortefi, "class NotMortefi: pass", use_custom=True)

        self.assertEqual(code_path.read_bytes(), original_code)
        self.assertEqual(modes_path.read_bytes(), original_modes)
        self.assertTrue(load_custom_char_class(Mortefi).custom_marker)

    def test_team_code_only_applies_to_matching_team(self):
        class Task:
            chars = []

        team = (Mortefi, Chixia, Verina)
        create_custom_team(team)
        code = """
from src.char.Mortefi import Mortefi as BuiltinMortefi


class Mortefi(BuiltinMortefi):
    custom_marker = "matching-team"
"""
        save_team_char_code(team, Mortefi, code)
        task = Task()
        task.chars = [
            Mortefi(task, 0, char_name=Labels.char_mortefi, confidence=0.99),
            Chixia(task, 1, char_name=Labels.char_chixia, confidence=0.99),
            Verina(task, 2, char_name=Labels.char_verina, confidence=0.99),
        ]

        apply_team_char_classes(task, task.chars)

        self.assertEqual(task.chars[0].custom_marker, "matching-team")

        other_task = Task()
        other_task.chars = [
            Mortefi(other_task, 0, char_name=Labels.char_mortefi, confidence=0.99),
            Chixia(other_task, 1, char_name=Labels.char_chixia, confidence=0.99),
            Baizhi(other_task, 2, char_name=Labels.char_baizhi, confidence=0.99),
        ]
        apply_team_char_classes(other_task, other_task.chars)
        self.assertIs(type(other_task.chars[0]), Mortefi)

    def test_team_logging_override_hot_loads_and_restores_existing_instances(self):
        class Task:
            pass

        team = (Mortefi, Chixia, Verina)
        create_custom_team(team)
        baseline = read_team_char_code(team, Mortefi)
        task = Task()
        task.chars = [
            Mortefi(task, 0, char_name=Labels.char_mortefi),
            Chixia(task, 1, char_name=Labels.char_chixia),
            Verina(task, 2, char_name=Labels.char_verina),
        ]
        original_class = type(task.chars[0])
        task.chars[0].last_res = 123.0
        diagnostic = """
from src.char.Mortefi import Mortefi as BuiltinMortefi

class Mortefi(BuiltinMortefi):
    evaluation_logging_marker = True
"""
        try:
            save_team_char_code(team, Mortefi, diagnostic)
            apply_team_char_classes(task, task.chars)
            self.assertIsNot(type(task.chars[0]), original_class)
            self.assertTrue(task.chars[0].evaluation_logging_marker)
            self.assertEqual(task.chars[0].last_res, 123.0)
        finally:
            save_team_char_code(team, Mortefi, baseline)
            apply_team_char_classes(task, task.chars)
        self.assertIs(type(task.chars[0]), original_class)
        self.assertFalse(hasattr(task.chars[0], 'evaluation_logging_marker'))
        self.assertEqual(task.chars[0].last_res, 123.0)
        self.assertEqual(read_team_char_code(team, Mortefi), baseline)

    def test_same_character_can_have_different_code_in_two_teams(self):
        first_team = (Mortefi, Chixia, Verina)
        second_team = (Mortefi, Chixia, Baizhi)
        create_custom_team(first_team)
        create_custom_team(second_team)
        first_code = self._team_code("first")
        second_code = self._team_code("second")
        save_team_char_code(first_team, Mortefi, first_code)
        save_team_char_code(second_team, Mortefi, second_code)

        class Task:
            chars = []

        first_task = Task()
        first_task.chars = self._chars(first_task, Verina)
        second_task = Task()
        second_task.chars = self._chars(second_task, Baizhi)
        apply_team_char_classes(first_task, first_task.chars)
        apply_team_char_classes(second_task, second_task.chars)

        self.assertEqual(first_task.chars[0].team_marker, "first")
        self.assertEqual(second_task.chars[0].team_marker, "second")
        self.assertIsNot(type(first_task.chars[0]), type(second_task.chars[0]))

    def test_list_custom_teams_reports_unreadable_team_folder(self):
        valid_team = (Mortefi, Chixia, Verina)
        create_custom_team(valid_team)
        unreadable_folder = Path(self.temp_dir.name) / "custom_teams" / "Aemeath__Augusta__Baizhi"
        unreadable_folder.mkdir()
        original_stat = Path.stat

        def stat(path, *args, **kwargs):
            if path == unreadable_folder:
                raise PermissionError(5, "Access is denied", str(path))
            return original_stat(path, *args, **kwargs)

        with patch.object(Path, 'stat', stat):
            with self.assertRaises(PermissionError):
                list_custom_teams()

        with patch.object(Path, 'iterdir', side_effect=PermissionError('合成工坊目录读取失败')):
            with self.assertRaises(PermissionError):
                list_custom_teams()
        # 普通辅助目录和导入暂存目录仍不构成工坊队伍。
        (unreadable_folder.parent / '.Chixia__Mortefi__Verina-temporary').mkdir()
        (unreadable_folder.parent / 'Chixia__Mortefi__Verina.backup').mkdir()
        (unreadable_folder.parent / 'notes').mkdir()
        self.assertEqual(list_custom_teams(), [tuple(sorted(
            (cls.__name__ for cls in valid_team), key=str.casefold))])

    def test_export_detected_team_without_saved_team_uses_builtin_code(self):
        team = (Mortefi, Chixia, Verina)
        archive = export_custom_team(
            team, self.temp_dir.name, name="Current", description="Rotation",
            author="Tester", version="1", allow_builtin=True,
        )
        info = inspect_team_archive(archive)
        for char_cls in team:
            self.assertEqual(info["codes"][char_cls.__name__], read_builtin_char_code(char_cls))
        self.assertEqual(list_custom_teams(), [])

    def test_export_and_import_team_archive(self):
        team = (Mortefi, Chixia, Verina)
        create_custom_team(team)
        archive = export_custom_team(
            team, self.temp_dir.name, name="My Team", description="Rotation",
            author="Tester", version="1.2.3",
        )
        self.assertEqual(archive.name, "My_Team_Tester_1.2.3.zip")
        with zipfile.ZipFile(archive) as exported:
            manifest = json.loads(exported.read("team.json"))
            self.assertEqual(set(manifest), {"name", "description", "author", "version", "team"})
            self.assertEqual(manifest["name"], "My_Team")
            self.assertEqual(manifest["team"], ", ".join(sorted(manifest["team"].split(", "), key=str.casefold)))

        info = inspect_team_archive(archive)
        self.assertNotIn("\r", info["codes"][Mortefi.__name__])
        import_custom_team(info)
        self.assertEqual(info["team"], tuple(sorted((cls.__name__ for cls in team), key=str.casefold)))
        self.assertNotIn(b"\r\r\n", get_team_char_file(team, Mortefi).read_bytes())

    def test_import_team_keeps_import_code_backup(self):
        team = (Mortefi, Chixia, Verina)
        create_custom_team(team)
        save_team_char_code(team, Mortefi, self._team_code("before import"))
        archive = export_custom_team(
            team, self.temp_dir.name, name="My Team", description="Rotation",
            author="Tester", version="1.2.3",
        )
        info = inspect_team_archive(archive)
        import_custom_team(info)

        self.assertEqual(get_team_code_mode(team), TEAM_CODE_MODE_IMPORT)
        for char_cls in team:
            imported = info["codes"][char_cls.__name__]
            self.assertEqual(read_team_char_code(team, char_cls), imported)
            self.assertEqual(read_team_import_code(team, char_cls), imported)

    def test_set_team_code_mode_keeps_team_manifest(self):
        team = (Mortefi, Chixia, Verina)
        create_custom_team(team)
        manifest_before = read_team_manifest(team)
        # 旧版清单允许 team 为数组；切换模式不能重建清单并丢失扩展字段。
        manifest_before['team'] = [char.__name__ for char in team]
        manifest_before['extension'] = {'enabled': False, 'count': 0, 'value': None}
        write_team_manifest(team, manifest_before)

        set_team_code_mode(team, TEAM_CODE_MODE_IMPORT)
        self.assertEqual(get_team_code_mode(team), TEAM_CODE_MODE_IMPORT)
        set_team_code_mode(team, TEAM_CODE_MODE_BUILTIN)
        self.assertEqual(get_team_code_mode(team), TEAM_CODE_MODE_BUILTIN)
        self.assertEqual(read_team_manifest(team)["code_mode"], TEAM_CODE_MODE_BUILTIN)
        self.assertEqual(read_team_manifest(team)["team"], manifest_before["team"])
        self.assertEqual(read_team_manifest(team)['extension'], manifest_before['extension'])

    def test_bad_team_manifest_fails_before_saving_code_or_mode(self):
        team = (Mortefi, Chixia, Verina)
        folder = create_custom_team(team)
        path = folder / 'team.json'
        codes = {file: file.read_bytes() for file in folder.glob('*.py')}
        for original in (b'{\r\n"unfinished":', b'[]', b'{"extension": NaN}'):
            with self.subTest(original=original):
                path.write_bytes(original)
                with self.assertRaises(ValueError):
                    read_team_manifest(team)
                with self.assertRaises(ValueError):
                    team_code_state(team)
                with self.assertRaises(ValueError):
                    normalize_custom_teams()
                with self.assertRaises(ValueError):
                    set_team_code_mode(team, TEAM_CODE_MODE_IMPORT)
                with self.assertRaises(ValueError):
                    save_team_code_as_import(team, {Mortefi.__name__: self._team_code('合成修改')})
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual({file: file.read_bytes() for file in folder.glob('*.py')}, codes)

    def test_team_manifest_io_failure_preserves_the_whole_team(self):
        team = (Mortefi, Chixia, Verina)
        folder = create_custom_team(team)
        path = folder / 'team.json'
        # 混合换行与 BOM 都属于用户原字节，失败回滚不能经过文本换行转换。
        original = b'\xef\xbb\xbf{\r\n"name": "legacy",\n"extension": {"enabled": false, "value": null}}'
        path.write_bytes(original)
        source = get_team_char_file(team, Mortefi)
        source.write_bytes(source.read_bytes().replace(b'\r\n', b'\n'))
        snapshots = {file: file.read_bytes() for file in folder.iterdir()}
        with patch('builtins.open', side_effect=PermissionError('合成读取拒绝')), \
                patch('io.open', side_effect=PermissionError('合成读取拒绝')):
            with self.assertRaises(PermissionError):
                set_team_code_mode(team, TEAM_CODE_MODE_IMPORT)
        self.assertEqual(path.read_bytes(), original)
        with patch('os.replace', side_effect=PermissionError('合成原子提交失败')):
            with self.assertRaises(PermissionError):
                save_team_code_as_import(team, {Mortefi.__name__: self._team_code('合成修改')})
        self.assertEqual({file: file.read_bytes() for file in folder.iterdir()}, snapshots)
        with self.assertRaises(ValueError):
            write_team_manifest(team, {'extension': float('nan')})
        self.assertEqual(path.read_bytes(), original)

    def test_team_code_state_requires_every_imported_copy(self):
        team = (Mortefi, Chixia, Verina)
        create_custom_team(team)
        self.assertEqual(team_code_state(team), TEAM_CODE_STATE_NONE)

        save_team_import_code(team, Mortefi, read_builtin_char_code(Mortefi))
        self.assertEqual(team_code_state(team), TEAM_CODE_STATE_NONE)

        for char_cls in team:
            save_team_import_code(team, char_cls, read_builtin_char_code(char_cls))
        self.assertEqual(team_code_state(team), TEAM_CODE_MODE_BUILTIN)
        set_team_code_mode(team, TEAM_CODE_MODE_IMPORT)
        self.assertEqual(team_code_state(team), TEAM_CODE_MODE_IMPORT)

    def test_imported_code_equal_to_built_in_code_is_still_imported(self):
        """An import may ship the built in code unchanged, the team still runs imported code."""
        team = (Mortefi, Chixia, Verina)
        create_custom_team(team)
        archive = export_custom_team(team, self.temp_dir.name, "Team", "Description", "Tester", "1")
        delete_custom_team(team)
        import_custom_team(inspect_team_archive(archive))

        self.assertEqual(team_code_state(team), TEAM_CODE_MODE_IMPORT)
        self.assertEqual(drifted_team_members(team), [])

    def test_normalize_fills_missing_copies_from_current_code(self):
        team = (Mortefi, Chixia, Verina)
        create_custom_team(team)
        imported_code = self._team_code("imported")
        save_team_import_code(team, Mortefi, imported_code)
        edited_code = read_builtin_char_code(Chixia) + "\n# edited\n"
        save_team_char_code(team, Chixia, edited_code)

        self.assertEqual(normalize_team_code(team), [Chixia.__name__, Verina.__name__])
        self.assertEqual(read_team_import_code(team, Mortefi), imported_code)
        self.assertEqual(read_team_import_code(team, Chixia), edited_code)
        self.assertEqual(read_team_import_code(team, Verina), read_builtin_char_code(Verina))
        self.assertEqual(team_code_state(team), TEAM_CODE_MODE_BUILTIN)
        self.assertEqual(normalize_team_code(team), [])

    def test_normalize_gives_an_import_mode_team_its_copies(self):
        team = (Mortefi, Chixia, Verina)
        create_custom_team(team)
        current_code = self._team_code("current")
        save_team_char_code(team, Mortefi, current_code)
        set_team_code_mode(team, TEAM_CODE_MODE_IMPORT)
        self.assertEqual(team_code_state(team), TEAM_CODE_STATE_NONE)

        normalize_custom_teams()

        self.assertEqual(team_code_state(team), TEAM_CODE_MODE_IMPORT)
        self.assertEqual(read_team_import_code(team, Mortefi), current_code)
        self.assertEqual(read_team_import_code(team, Chixia), read_builtin_char_code(Chixia))

    def test_normalize_leaves_a_never_imported_team_alone(self):
        team = (Mortefi, Chixia, Verina)
        create_custom_team(team)
        save_team_char_code(team, Mortefi, self._team_code("local"))

        self.assertEqual(normalize_team_code(team), [])
        self.assertEqual(team_code_state(team), TEAM_CODE_STATE_NONE)
        self.assertFalse(get_team_import_file(team, Mortefi).is_file())

    def test_switch_team_code_mode_keeps_the_imported_copy(self):
        team = (Mortefi, Chixia, Verina)
        self._import_team(team)

        switch_team_code_mode(team, TEAM_CODE_MODE_BUILTIN)
        for char_cls in team:
            self.assertEqual(read_team_char_code(team, char_cls), read_builtin_char_code(char_cls))
            self.assertEqual(read_team_import_code(team, char_cls), self._char_code(char_cls, "imported"))
        self.assertEqual(team_code_state(team), TEAM_CODE_MODE_BUILTIN)

        switch_team_code_mode(team, TEAM_CODE_MODE_IMPORT)
        for char_cls in team:
            self.assertEqual(read_team_char_code(team, char_cls), self._char_code(char_cls, "imported"))
        self.assertEqual(team_code_state(team), TEAM_CODE_MODE_IMPORT)

    def test_switch_team_code_mode_rolls_the_whole_team_back_on_failure(self):
        team = (Mortefi, Chixia, Verina)
        self._import_team(team)
        source = get_team_char_file(team, Mortefi)
        original = source.read_bytes().replace(b'\r\n', b'\n').replace(b'\n', b'\r\n', 1)
        source.write_bytes(original)
        snapshots = {path: path.read_bytes() for path in source.parent.iterdir()}

        with patch("src.char.CustomCharLoader._load_team_char_class_from_file",
                   side_effect=[None, RuntimeError("broken member")]):
            with self.assertRaises(RuntimeError):
                switch_team_code_mode(team, TEAM_CODE_MODE_BUILTIN)

        for char_cls in team:
            self.assertEqual(read_team_char_code(team, char_cls), self._char_code(char_cls, "imported"))
        self.assertEqual(get_team_code_mode(team), TEAM_CODE_MODE_IMPORT)
        self.assertEqual({path: path.read_bytes() for path in source.parent.iterdir()}, snapshots)

        # 单个队伍成员的保存入口也必须遵守相同的原字节回滚边界。
        with self.assertRaises(RuntimeError):
            save_team_char_code(team, Mortefi, 'class Mortefi: pass')
        self.assertEqual({path: path.read_bytes() for path in source.parent.iterdir()}, snapshots)

    def test_switch_team_code_mode_refuses_a_never_imported_team(self):
        team = (Mortefi, Chixia, Verina)
        create_custom_team(team)
        with self.assertRaises(ValueError):
            switch_team_code_mode(team, TEAM_CODE_MODE_IMPORT)
        self.assertEqual(team_code_state(team), TEAM_CODE_STATE_NONE)

    def test_save_as_import_gives_a_never_imported_team_imported_code(self):
        team = (Mortefi, Chixia, Verina)
        create_custom_team(team)
        code = self._team_code("first save")

        save_team_code_as_import(team, {Mortefi.__name__: code})

        self.assertEqual(team_code_state(team), TEAM_CODE_MODE_IMPORT)
        self.assertEqual(read_team_char_code(team, Mortefi), code)
        self.assertEqual(read_team_import_code(team, Mortefi), code)
        for char_cls in (Chixia, Verina):
            self.assertEqual(read_team_import_code(team, char_cls), read_builtin_char_code(char_cls))

    def test_save_as_import_on_built_in_code_replaces_the_whole_team_import(self):
        team = (Mortefi, Chixia, Verina)
        self._import_team(team)
        switch_team_code_mode(team, TEAM_CODE_MODE_BUILTIN)
        code = self._team_code("edited on built in")
        codes = {Mortefi.__name__: code}

        self.assertEqual(changed_import_members(team, codes),
                         [Chixia.__name__, Mortefi.__name__, Verina.__name__])
        save_team_code_as_import(team, codes)

        self.assertEqual(team_code_state(team), TEAM_CODE_MODE_IMPORT)
        self.assertEqual(read_team_import_code(team, Mortefi), code)
        self.assertEqual(read_team_import_code(team, Chixia), read_builtin_char_code(Chixia))

    def test_switch_all_skips_teams_whose_code_neither_side_keeps(self):
        team = (Mortefi, Chixia, Verina)
        self._import_team(team)
        switch_team_code_mode(team, TEAM_CODE_MODE_BUILTIN)
        local_code = self._team_code("local")
        save_team_char_code(team, Mortefi, local_code)
        self.assertEqual(drifted_team_members(team), [Mortefi.__name__])

        summary = switch_all_teams_code_mode(TEAM_CODE_MODE_IMPORT)

        self.assertEqual(summary["drifted"], [(Chixia.__name__, Mortefi.__name__, Verina.__name__)])
        self.assertEqual(summary["switched"], [])
        self.assertEqual(read_team_char_code(team, Mortefi), local_code)
        self.assertEqual(get_team_code_mode(team), TEAM_CODE_MODE_BUILTIN)

    def test_export_ships_the_imported_code_while_built_in_code_runs(self):
        team = (Mortefi, Chixia, Verina)
        self._import_team(team)
        switch_team_code_mode(team, TEAM_CODE_MODE_BUILTIN)

        archive = export_custom_team(team, self.temp_dir.name, "Team", "Description", "Tester", "1")

        self.assertEqual(inspect_team_archive(archive)["codes"][Mortefi.__name__],
                         self._char_code(Mortefi, "imported"))

    def test_read_team_import_code_requires_backup(self):
        team = (Mortefi, Chixia, Verina)
        create_custom_team(team)
        with self.assertRaises(ValueError):
            read_team_import_code(team, Mortefi)

    def test_save_team_import_code_rejects_char_outside_team(self):
        team = (Mortefi, Chixia, Verina)
        create_custom_team(team)
        with self.assertRaises(ValueError):
            save_team_import_code(team, Baizhi, "class Baizhi: pass")

    @staticmethod
    def _team_code(marker):
        return f'''\nfrom src.char.Mortefi import Mortefi as BuiltinMortefi\n\n\nclass Mortefi(BuiltinMortefi):\n    team_marker = "{marker}"\n'''

    @staticmethod
    def _char_code(char_cls, marker):
        name = char_cls.__name__
        return (f"from src.char.{name} import {name} as Builtin{name}\n\n\n"
                f"class {name}(Builtin{name}):\n    team_marker = \"{marker}\"\n")

    def _import_team(self, team):
        """Bring `team` in the way an archive import does: imported code in effect."""
        import_custom_team({
            "team": team, "manifest": {},
            "codes": {char_cls.__name__: self._char_code(char_cls, "imported") for char_cls in team},
        })

    @staticmethod
    def _chars(task, third_cls):
        third_label = Labels.char_verina if third_cls is Verina else Labels.char_baizhi
        return [
            Mortefi(task, 0, char_name=Labels.char_mortefi, confidence=0.99),
            Chixia(task, 1, char_name=Labels.char_chixia, confidence=0.99),
            third_cls(task, 2, char_name=third_label, confidence=0.99),
        ]


if __name__ == "__main__":
    unittest.main()
