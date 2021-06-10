from pdb import Pdb
from unittest.mock import Mock
from typing import Literal, Optional, Dict, Any, Tuple, Generator
from types import FrameType
from importlib import import_module


VariableDescription = Literal['global', 'local']


def import_string(dotted_path):
    """
    Import a dotted module path and return the attribute/class designated by the
    last name in the path. Raise ImportError if the import failed.
    Taken from django.utils.module_loading with adjustment to allow modules to be given, not just attributes and classes
    """
    try:
        module_path, class_name = dotted_path.rsplit('.', 1)
    except ValueError as err:
        return import_module(dotted_path)

    module = import_module(module_path)

    try:
        return getattr(module, class_name)
    except AttributeError as err:
        raise ImportError('Module "%s" does not define a "%s" attribute/class' % (
            module_path, class_name)
        ) from err


class ExtendedPdb(Pdb):
    _locals = None
    _globals = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.monitor_mocks = {}
        self._mock_calls = {}

    @staticmethod
    def get_frame_details(frame: FrameType) -> Dict[str, Any]:
        return {
            'back': frame.f_back,
            'code': frame.f_code,
            'locals': frame.f_locals,
            'globals': frame.f_globals,
            'builtins': frame.f_builtins,
            'lasti': frame.f_lasti,
            'lineno': frame.f_lineno
        }

    def runmodule(self, module_name: str) -> None:
        return self._runmodule(module_name)

    def print_message(self, msg: str) -> None:
        print(f'{self.curframe.f_lineno}: {msg}')

    def on_new_variable(self, name: str, value: Any, desc: VariableDescription):
        self.print_message(f'{desc} variable {name} has been created with value {value}')

    def on_variable_changed(self, name: str, value: Any, old_value: Any, desc: VariableDescription):
        self.print_message(f'{desc} variable {name} has changed from {old_value} to {value}')

    def on_variable_deleted(self, name: str, old_value: Any, desc: VariableDescription):
        self.print_message(f'{desc} variable {name} has been deleted')

    def on_new_side_effect(self, attr: str, call: Tuple, mock: Mock) -> None:
        self.print_message(f'{attr} was called with args: {call}')

    def _monitor_changes(self, prev: Optional[Dict[str, Any]], current: Dict[str, Any], desc: VariableDescription) -> None:
        if prev:
            for k, v in current.items():
                if k not in prev:
                    self.on_new_variable(k, v, desc)
                if v != prev[k]:
                    self.on_variable_changed(k, v, prev[k], desc)
        for k, v in prev.items():
            if k not in current:
                self.on_variable_deleted(k, v, desc)

    def _check_mock(self, name: str, mock: Mock):
        prev_calls = self._mock_calls.get(name, [])
        new_calls = mock.call_args_list[mock.call_args_list[len(prev_calls):]]
        for call in new_calls:
            self.on_new_side_effect(name, call, mock)
        self._mock_calls[name] = mock.call_args_list.copy()

    def _check_side_effects(self):
        for arg, module in self.monitor_mocks.items():
            self._check_mock(arg, module)

    def _register_mocks(self, arg: str, module: object) -> Generator[Tuple[str, Mock], None, None]:
        if isinstance(module, Mock):
            self.monitor_mocks[arg] = module
        else:
            for name in dir(module):
                attr = getattr(module, name, None)
                if isinstance(attr, Mock):
                    mock_name = f'{arg}.{name}'
                    self.monitor_mocks[mock_name] = attr
                    yield mock_name, attr

    def _register_mocks_from_string(self, path: str) -> Generator[Tuple[str, Mock], None, None]:
        module = import_string(path)
        yield from self._register_mocks(path, module)

    def do_pf_globals_changes(self, arg: str):
        self._monitor_changes(self._globals, self.curframe.f_globals, 'global')
        self._locals = self.curframe.f_globals.copy()

    def do_pf_locals_changes(self, arg: str):
        self._monitor_changes(self._locals, self.curframe.f_globals, 'local')
        self._locals = self.curframe.f_locals.copy()

    def do_pf_side_effect(self, arg: str):
        for name, module in self._register_mocks_from_string(arg):
            self._check_mock(name, module)

    def do_register_mocks(self, arg: str):
        for mock in arg.split(','):
            self._register_mocks_from_string(mock.strip())

    def do_pf_side_effects(self, arg: str):
        self._check_side_effects()


class AutomatedPdb(ExtendedPdb):
    def __init__(self, function: str, cmdloop: bool = False, all_lines: bool = True, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rcLines.extend([f"tbreak {function}", "cont"])
        self.launch_cmdloop = False
        self.all_lines = True
        self.monitor_mocks = {}
        self._mock_calls = {}

    def _cmdloop(self) -> None:
        if self.launch_cmdloop:
            super()._cmdloop()

    def user_call(self, frame: FrameType, argument_list: None) -> None:
        if not self.rcLines:
            self.rcLines.extend(['args', 'pf_side_effects'])
            if not self.launch_cmdloop:
                self.rcLines.append('next' if self.all_lines else 'cont')
        super().user_call(frame, argument_list)

    def user_line(self, frame: FrameType) -> None:
        if not self.rcLines:
            self.rcLines.extend(['args', 'do_pf_locals_changes', 'do_pf_globals_changes', 'pf_side_effects'])
            if not self.launch_cmdloop:
                self.rcLines.append('next')
        super().user_line(frame)

    def user_return(self, frame: FrameType, return_value: Any) -> None:
        if not self.rcLines:
            self.rcLines.extend(['args', 'pf_side_effects'])
            if not self.launch_cmdloop:
                self.rcLines.append('cont')
        super().user_line(frame)


def setup_mocks():
    pass


if __name__ == '__main__':
    script = "script"
    function = "validate"
    condition = ""
    setup_mocks()
    dbg = AutomatedPdb(function)
    dbg._runmodule(script)
