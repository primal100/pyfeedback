import argparse
from pdb import Pdb
from importlib import import_module
from unittest.mock import Mock, AsyncMock
import inspect
from functools import partial
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from typing import Optional, Dict, Any, Tuple, Generator, List
from types import FrameType


try:
    # >= Python 3.8
    from typing import Literal
except ImportError:
    # < Python 3.8
    from typing_extensions import Literal

try:
    # >= Python 3.9
    from functools import cache
except ImportError:
    # < Python 3.9
    from functools import lru_cache as cache


VariableDescription = Literal['global', 'local']


@cache
def import_string(dotted_path) -> object:
    """
    Import a dotted module path and return the attribute/class designated by the
    last name in the path. Raise ImportError if the import failed.
    Taken from django.utils.module_loading with adjustment to allow modules to be returned, not just attributes and classes
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
        self._modules_with_mocks: Dict[str, object] = {}
        self._monitor_mocks = {}
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

    @staticmethod
    def _find_mocks_in_object(arg: str, obj: object) -> Generator[Tuple[str, Mock], None, None]:
        if isinstance(obj, Mock):
            yield arg, obj
        else:
            for name in dir(obj):
                attr = getattr(obj, name, None)
                if isinstance(attr, Mock):
                    mock_name = f'{arg}.{name}'
                    yield mock_name, attr

    def _find_mocks(self) -> Generator[Tuple[str, Mock], None, None]:
        for name, obj in self._modules_with_mocks.items():
            yield from self._find_mocks_in_object(name, obj)

    def _check_side_effects(self):
        for arg, mock in self._find_mocks():
            self._check_mock(arg, mock)

    def _register_mock_module_from_string(self, path: str) -> object:
        if path not in self._modules_with_mocks:
            obj = import_string(path)
            self._modules_with_mocks[path] = obj
            return obj
        return self._modules_with_mocks[path]

    def do_pf_globals_changes(self, arg: str):
        self._monitor_changes(self._globals, self.curframe.f_globals, 'global')
        self._locals = self.curframe.f_globals.copy()

    def do_pf_locals_changes(self, arg: str):
        self._monitor_changes(self._locals, self.curframe.f_globals, 'local')
        self._locals = self.curframe.f_locals.copy()

    def do_pf_side_effect(self, arg: str):
        obj = self._register_mock_module_from_string(arg)
        for name, mock in self._find_mocks_in_object(arg, obj):
            self._check_mock(arg, mock)

    def add_mocks(self, arg: str, keep_functionality: bool = True):
        for item in arg.split(','):
            attr = item.split('.')[-1]
            module_string = arg.split(f'.{attr}')[0]
            obj = self._register_mock_module_from_string(module_string)
            value = getattr(obj, attr, None)
            if value:
                mock_class = AsyncMock if inspect.iscoroutinefunction(value) else Mock
                side_effect = value if keep_functionality else None
                setattr(obj, attr, mock_class(side_effect=side_effect))
                print(f'Replaced {arg} with{mock_class.__name__}')
            else:
                print(f'There is no attribute called {attr} in {module_string}')

    def do_pf_add_functional_mocks(self, arg: str):
        self.add_mocks(arg, keep_functionality=True)

    def do_pf_add_mocks(self, arg: str):
        self.add_mocks(arg, keep_functionality=False)

    def do_pf_register_mocks(self, arg: str):
        for mock in arg.split(','):
            self._register_mock_module_from_string(mock.strip())

    def do_pf_side_effects(self, arg: str):
        self._check_side_effects()


class AutomatedPdb(ExtendedPdb):
    def __init__(self, function: str, cmdloop: bool = False, all_lines: bool = True, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rcLines.extend([f"tbreak {function}", "cont"])
        self.launch_cmdloop = False
        self.all_lines = True

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


class ScriptFileHandler(FileSystemEventHandler):
    def __init__(self, debugger: ExtendedPdb, module: str):
        self.debugger = debugger
        self._set_module_name(module)
        self.debugger._runmodule(self.module)

    def _set_module_name(self, script: str) -> None:
        self.module = script.split('.py')[0]

    def _reload(self):
        self.debugger.do_quit("")
        self.debugger._runmodule(self.module)

    def on_moved(self, event):
        print(f'Script was renamed to {event.dest_path}. Reloading.')
        self._set_module_name(event.dest_path)
        self._reload()

    def on_deleted(self, event):
        print(f'Script was deleted. Quitting')
        self.debugger.do_quit("")

    def on_modified(self, event):
        print(f'Script was modified. Reloading.')
        self._reload()


def run_configuration(script: str, interactive: bool, breakpoints: List[str], tbreakpoints: List[str],
                      commands: List[str], register_mocks: List[str], add_mocks: List[str],
                      add_functional_mocks: List[str], watch: bool):
    pdb_class = ExtendedPdb if interactive else AutomatedPdb
    initial_commands = [f'tbreak {arg}' for arg in tbreakpoints] + [f'break {arg}' for arg in breakpoints]
    dbg = pdb_class(initial_commands, commands)
    for arg in add_mocks:
        dbg.do_pf_add_mocks(arg)
    for arg in add_functional_mocks:
        dbg.do_pf_add_functional_mocks(arg)
    for arg in register_mocks:
        dbg.do_pf_register_mocks(arg)
    if watch:
        observer = Observer()
        observer.schedule(partial(ScriptFileHandler, dbg, script), script)
    else:
        dbg._runmodule(script.split('.py')[-0])


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('script', type=str, help="Python script to debug"),
    parser.add_argument('-i', '--interactive', action='store_true', help='Open an interactive session.'),
    parser.add_argument('-b, --breakpoint', type=str, help=f'Set a breakpoint', nargs="*"),
    parser.add_argument('-t, --tbreakpoint', type=str, help=f'Set a once-off breakpoint', nargs="*"),
    parser.add_argument('-c', '--command', type=str, nargs='*',
                        help='Commands to run at each breakpoint')
    parser.add_argument('-r', '--register-mocks', type=str, nargs='*',
                        help='Register modules or classes which may contain mocks in comma separated lists')
    parser.add_argument('-a', '--add-mocks', type=str, nargs='*',
                        help='Add a mock')
    parser.add_argument('-f', '--add-functional-mocks', type=str, nargs='*',
                        help='Add a mock but retain existing functionality')
    parser.add_argument('-w', '--watch', action='store_true',
                        help='Watch for file changes and reload debugger accordingly')
    args, kw = parser.parse_known_args()
    run_configuration(
        args.script,
        args.interactive,
        args.breakpoint,
        args.tbreakpoint,
        args.command,
        args.register_mocks,
        args.add_mocks,
        args.add_functional_mocks,
        args.watch
    )
