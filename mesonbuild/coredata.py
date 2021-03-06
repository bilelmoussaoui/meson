# Copyright 2012-2019 The Meson development team

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from . import mlog
import pickle, os, uuid, shlex
import sys
from itertools import chain
from pathlib import PurePath
from collections import OrderedDict
from .mesonlib import (
    MesonException, MachineChoice, PerMachine,
    default_libdir, default_libexecdir, default_prefix
)
from .wrap import WrapMode
import ast
import argparse
import configparser
from typing import Optional, Any, TypeVar, Generic, Type, List, Union
import typing
import enum

if typing.TYPE_CHECKING:
    from . import dependencies

version = '0.50.999'
backendlist = ['ninja', 'vs', 'vs2010', 'vs2015', 'vs2017', 'vs2019', 'xcode']

default_yielding = False

# Can't bind this near the class method it seems, sadly.
_T = TypeVar('_T')

class UserOption(Generic[_T]):
    def __init__(self, description, choices, yielding):
        super().__init__()
        self.choices = choices
        self.description = description
        if yielding is None:
            yielding = default_yielding
        if not isinstance(yielding, bool):
            raise MesonException('Value of "yielding" must be a boolean.')
        self.yielding = yielding

    def printable_value(self):
        return self.value

    # Check that the input is a valid value and return the
    # "cleaned" or "native" version. For example the Boolean
    # option could take the string "true" and return True.
    def validate_value(self, value: Any) -> _T:
        raise RuntimeError('Derived option class did not override validate_value.')

    def set_value(self, newvalue):
        self.value = self.validate_value(newvalue)

class UserStringOption(UserOption[str]):
    def __init__(self, description, value, choices=None, yielding=None):
        super().__init__(description, choices, yielding)
        self.set_value(value)

    def validate_value(self, value):
        if not isinstance(value, str):
            raise MesonException('Value "%s" for string option is not a string.' % str(value))
        return value

class UserBooleanOption(UserOption[bool]):
    def __init__(self, description, value, yielding=None):
        super().__init__(description, [True, False], yielding)
        self.set_value(value)

    def __bool__(self) -> bool:
        return self.value

    def validate_value(self, value) -> bool:
        if isinstance(value, bool):
            return value
        if value.lower() == 'true':
            return True
        if value.lower() == 'false':
            return False
        raise MesonException('Value %s is not boolean (true or false).' % value)

class UserIntegerOption(UserOption[int]):
    def __init__(self, description, min_value, max_value, value, yielding=None):
        super().__init__(description, [True, False], yielding)
        self.min_value = min_value
        self.max_value = max_value
        self.set_value(value)
        c = []
        if min_value is not None:
            c.append('>=' + str(min_value))
        if max_value is not None:
            c.append('<=' + str(max_value))
        self.choices = ', '.join(c)

    def validate_value(self, value) -> int:
        if isinstance(value, str):
            value = self.toint(value)
        if not isinstance(value, int):
            raise MesonException('New value for integer option is not an integer.')
        if self.min_value is not None and value < self.min_value:
            raise MesonException('New value %d is less than minimum value %d.' % (value, self.min_value))
        if self.max_value is not None and value > self.max_value:
            raise MesonException('New value %d is more than maximum value %d.' % (value, self.max_value))
        return value

    def toint(self, valuestring) -> int:
        try:
            return int(valuestring)
        except ValueError:
            raise MesonException('Value string "%s" is not convertable to an integer.' % valuestring)

class UserUmaskOption(UserIntegerOption, UserOption[Union[str, int]]):
    def __init__(self, description, value, yielding=None):
        super().__init__(description, 0, 0o777, value, yielding)
        self.choices = ['preserve', '0000-0777']

    def printable_value(self):
        if self.value == 'preserve':
            return self.value
        return format(self.value, '04o')

    def validate_value(self, value):
        if value is None or value == 'preserve':
            return 'preserve'
        return super().validate_value(value)

    def toint(self, valuestring):
        try:
            return int(valuestring, 8)
        except ValueError as e:
            raise MesonException('Invalid mode: {}'.format(e))

class UserComboOption(UserOption[str]):
    def __init__(self, description, choices: List[str], value, yielding=None):
        super().__init__(description, choices, yielding)
        if not isinstance(self.choices, list):
            raise MesonException('Combo choices must be an array.')
        for i in self.choices:
            if not isinstance(i, str):
                raise MesonException('Combo choice elements must be strings.')
        self.set_value(value)

    def validate_value(self, value):
        if value not in self.choices:
            optionsstring = ', '.join(['"%s"' % (item,) for item in self.choices])
            raise MesonException('Value "%s" for combo option is not one of the choices. Possible choices are: %s.' % (value, optionsstring))
        return value

class UserArrayOption(UserOption[List[str]]):
    def __init__(self, description, value, shlex_split=False, user_input=False, allow_dups=False, **kwargs):
        super().__init__(description, kwargs.get('choices', []), yielding=kwargs.get('yielding', None))
        self.shlex_split = shlex_split
        self.allow_dups = allow_dups
        self.value = self.validate_value(value, user_input=user_input)

    def validate_value(self, value, user_input=True) -> List[str]:
        # User input is for options defined on the command line (via -D
        # options). Users can put their input in as a comma separated
        # string, but for defining options in meson_options.txt the format
        # should match that of a combo
        if not user_input and isinstance(value, str) and not value.startswith('['):
            raise MesonException('Value does not define an array: ' + value)

        if isinstance(value, str):
            if value.startswith('['):
                newvalue = ast.literal_eval(value)
            elif value == '':
                newvalue = []
            else:
                if self.shlex_split:
                    newvalue = shlex.split(value)
                else:
                    newvalue = [v.strip() for v in value.split(',')]
        elif isinstance(value, list):
            newvalue = value
        else:
            raise MesonException('"{0}" should be a string array, but it is not'.format(str(newvalue)))

        if not self.allow_dups and len(set(newvalue)) != len(newvalue):
            msg = 'Duplicated values in array option is deprecated. ' \
                  'This will become a hard error in the future.'
            mlog.deprecation(msg)
        for i in newvalue:
            if not isinstance(i, str):
                raise MesonException('String array element "{0}" is not a string.'.format(str(newvalue)))
        if self.choices:
            bad = [x for x in newvalue if x not in self.choices]
            if bad:
                raise MesonException('Options "{}" are not in allowed choices: "{}"'.format(
                    ', '.join(bad), ', '.join(self.choices)))
        return newvalue


class UserFeatureOption(UserComboOption):
    static_choices = ['enabled', 'disabled', 'auto']

    def __init__(self, description, value, yielding=None):
        super().__init__(description, self.static_choices, value, yielding)

    def is_enabled(self):
        return self.value == 'enabled'

    def is_disabled(self):
        return self.value == 'disabled'

    def is_auto(self):
        return self.value == 'auto'


def load_configs(filenames: List[str]) -> configparser.ConfigParser:
    """Load configuration files from a named subdirectory."""
    config = configparser.ConfigParser()
    config.read(filenames)
    return config


if typing.TYPE_CHECKING:
    CacheKeyType = typing.Tuple[typing.Tuple[typing.Any, ...], ...]
    SubCacheKeyType = typing.Tuple[typing.Any, ...]


class DependencyCacheType(enum.Enum):

    OTHER = 0
    PKG_CONFIG = 1

    @classmethod
    def from_type(cls, dep: 'dependencies.Dependency') -> 'DependencyCacheType':
        from . import dependencies
        # As more types gain search overrides they'll need to be added here
        if isinstance(dep, dependencies.PkgConfigDependency):
            return cls.PKG_CONFIG
        return cls.OTHER


class DependencySubCache:

    def __init__(self, type_: DependencyCacheType):
        self.types = [type_]
        self.__cache = {}  # type: typing.Dict[SubCacheKeyType, dependencies.Dependency]

    def __getitem__(self, key: 'SubCacheKeyType') -> 'dependencies.Dependency':
        return self.__cache[key]

    def __setitem__(self, key: 'SubCacheKeyType', value: 'dependencies.Dependency') -> None:
        self.__cache[key] = value

    def __contains__(self, key: 'SubCacheKeyType') -> bool:
        return key in self.__cache

    def values(self) -> typing.Iterable['dependencies.Dependency']:
        return self.__cache.values()


class DependencyCache:

    """Class that stores a cache of dependencies.

    This class is meant to encapsulate the fact that we need multiple keys to
    successfully lookup by providing a simple get/put interface.
    """

    def __init__(self, builtins: typing.Dict[str, UserOption[typing.Any]], cross: bool):
        self.__cache = OrderedDict()  # type: typing.MutableMapping[CacheKeyType, DependencySubCache]
        self.__builtins = builtins
        self.__is_cross = cross

    def __calculate_subkey(self, type_: DependencyCacheType) -> typing.Tuple[typing.Any, ...]:
        if type_ is DependencyCacheType.PKG_CONFIG:
            if self.__is_cross:
                return tuple(self.__builtins['cross_pkg_config_path'].value)
            return tuple(self.__builtins['pkg_config_path'].value)
        assert type_ is DependencyCacheType.OTHER, 'Someone forgot to update subkey calculations for a new type'
        return tuple()

    def __iter__(self) -> typing.Iterator['CacheKeyType']:
        return self.keys()

    def put(self, key: 'CacheKeyType', dep: 'dependencies.Dependency') -> None:
        t = DependencyCacheType.from_type(dep)
        if key not in self.__cache:
            self.__cache[key] = DependencySubCache(t)
        subkey = self.__calculate_subkey(t)
        self.__cache[key][subkey] = dep

    def get(self, key: 'CacheKeyType') -> typing.Optional['dependencies.Dependency']:
        """Get a value from the cache.

        If there is no cache entry then None will be returned.
        """
        try:
            val = self.__cache[key]
        except KeyError:
            return None

        for t in val.types:
            subkey = self.__calculate_subkey(t)
            try:
                return val[subkey]
            except KeyError:
                pass
        return None

    def values(self) -> typing.Iterator['dependencies.Dependency']:
        for c in self.__cache.values():
            yield from c.values()

    def keys(self) -> typing.Iterator['CacheKeyType']:
        return iter(self.__cache.keys())

    def items(self) -> typing.Iterator[typing.Tuple['CacheKeyType', typing.List['dependencies.Dependency']]]:
        for k, v in self.__cache.items():
            vs = []
            for t in v.types:
                subkey = self.__calculate_subkey(t)
                if subkey in v:
                    vs.append(v[subkey])
            yield k, vs

    def clear(self) -> None:
        self.__cache.clear()

# This class contains all data that must persist over multiple
# invocations of Meson. It is roughly the same thing as
# cmakecache.

class CoreData:

    def __init__(self, options):
        self.lang_guids = {
            'default': '8BC9CEB8-8B4A-11D0-8D11-00A0C91BC942',
            'c': '8BC9CEB8-8B4A-11D0-8D11-00A0C91BC942',
            'cpp': '8BC9CEB8-8B4A-11D0-8D11-00A0C91BC942',
            'test': '3AC096D0-A1C2-E12C-1390-A8335801FDAB',
            'directory': '2150E333-8FDC-42A3-9474-1A3956D46DE8',
        }
        self.test_guid = str(uuid.uuid4()).upper()
        self.regen_guid = str(uuid.uuid4()).upper()
        self.install_guid = str(uuid.uuid4()).upper()
        self.target_guids = {}
        self.version = version
        self.init_builtins()
        self.backend_options = {}
        self.user_options = {}
        self.compiler_options = PerMachine({}, {})
        self.base_options = {}
        self.cross_files = self.__load_config_files(options.cross_file, 'cross')
        self.compilers = OrderedDict()
        self.cross_compilers = OrderedDict()

        build_cache = DependencyCache(self.builtins, False)
        if self.cross_files:
            host_cache = DependencyCache(self.builtins, True)
        else:
            host_cache = build_cache
        self.deps = PerMachine(build_cache, host_cache)  # type: PerMachine[DependencyCache]

        self.compiler_check_cache = OrderedDict()
        # Only to print a warning if it changes between Meson invocations.
        self.config_files = self.__load_config_files(options.native_file, 'native')
        self.libdir_cross_fixup()

    @staticmethod
    def __load_config_files(filenames: Optional[List[str]], ftype: str) -> List[str]:
        # Need to try and make the passed filenames absolute because when the
        # files are parsed later we'll have chdir()d.
        if not filenames:
            return []

        real = []
        for f in filenames:
            f = os.path.expanduser(os.path.expandvars(f))
            if os.path.exists(f):
                real.append(os.path.abspath(f))
                continue
            elif sys.platform != 'win32':
                paths = [
                    os.environ.get('XDG_DATA_HOME', os.path.expanduser('~/.local/share')),
                ] + os.environ.get('XDG_DATA_DIRS', '/usr/local/share:/usr/share').split(':')
                for path in paths:
                    path_to_try = os.path.join(path, 'meson', ftype, f)
                    if os.path.isfile(path_to_try):
                        real.append(path_to_try)
                        break
                else:
                    raise MesonException('Cannot find specified {} file: {}'.format(ftype, f))
                continue

            raise MesonException('Cannot find specified {} file: {}'.format(ftype, f))
        return real

    def libdir_cross_fixup(self):
        # By default set libdir to "lib" when cross compiling since
        # getting the "system default" is always wrong on multiarch
        # platforms as it gets a value like lib/x86_64-linux-gnu.
        if self.cross_files:
            self.builtins['libdir'].value = 'lib'

    def sanitize_prefix(self, prefix):
        if not os.path.isabs(prefix):
            raise MesonException('prefix value {!r} must be an absolute path'
                                 ''.format(prefix))
        if prefix.endswith('/') or prefix.endswith('\\'):
            # On Windows we need to preserve the trailing slash if the
            # string is of type 'C:\' because 'C:' is not an absolute path.
            if len(prefix) == 3 and prefix[1] == ':':
                pass
            # If prefix is a single character, preserve it since it is
            # the root directory.
            elif len(prefix) == 1:
                pass
            else:
                prefix = prefix[:-1]
        return prefix

    def sanitize_dir_option_value(self, prefix, option, value):
        '''
        If the option is an installation directory option and the value is an
        absolute path, check that it resides within prefix and return the value
        as a path relative to the prefix.

        This way everyone can do f.ex, get_option('libdir') and be sure to get
        the library directory relative to prefix.
        '''
        if option.endswith('dir') and os.path.isabs(value) and \
           option not in builtin_dir_noprefix_options:
            # Value must be a subdir of the prefix
            # commonpath will always return a path in the native format, so we
            # must use pathlib.PurePath to do the same conversion before
            # comparing.
            if os.path.commonpath([value, prefix]) != str(PurePath(prefix)):
                m = 'The value of the {!r} option is {!r} which must be a ' \
                    'subdir of the prefix {!r}.\nNote that if you pass a ' \
                    'relative path, it is assumed to be a subdir of prefix.'
                raise MesonException(m.format(option, value, prefix))
            # Convert path to be relative to prefix
            skip = len(prefix) + 1
            value = value[skip:]
        return value

    def init_builtins(self):
        # Create builtin options with default values
        self.builtins = {}
        for key, opt in builtin_options.items():
            self.builtins[key] = opt.init_option()
            if opt.separate_cross:
                self.builtins['cross_' + key] = opt.init_option()

    def init_backend_options(self, backend_name):
        if backend_name == 'ninja':
            self.backend_options['backend_max_links'] = \
                UserIntegerOption(
                    'Maximum number of linker processes to run or 0 for no '
                    'limit',
                    0, None, 0)
        elif backend_name.startswith('vs'):
            self.backend_options['backend_startup_project'] = \
                UserStringOption(
                    'Default project to execute in Visual Studio',
                    '')

    def get_builtin_option(self, optname):
        if optname in self.builtins:
            v = self.builtins[optname]
            if optname == 'wrap_mode':
                return WrapMode.from_string(v.value)
            return v.value
        raise RuntimeError('Tried to get unknown builtin option %s.' % optname)

    def set_builtin_option(self, optname, value):
        if optname == 'prefix':
            value = self.sanitize_prefix(value)
        elif optname in self.builtins:
            prefix = self.builtins['prefix'].value
            value = self.sanitize_dir_option_value(prefix, optname, value)
        else:
            raise RuntimeError('Tried to set unknown builtin option %s.' % optname)
        self.builtins[optname].set_value(value)

        # Make sure that buildtype matches other settings.
        if optname == 'buildtype':
            self.set_others_from_buildtype(value)
        else:
            self.set_buildtype_from_others()

    def set_others_from_buildtype(self, value):
        if value == 'plain':
            opt = '0'
            debug = False
        elif value == 'debug':
            opt = '0'
            debug = True
        elif value == 'debugoptimized':
            opt = '2'
            debug = True
        elif value == 'release':
            opt = '3'
            debug = False
        elif value == 'minsize':
            opt = 's'
            debug = True
        else:
            assert(value == 'custom')
            return
        self.builtins['optimization'].set_value(opt)
        self.builtins['debug'].set_value(debug)

    def set_buildtype_from_others(self):
        opt = self.builtins['optimization'].value
        debug = self.builtins['debug'].value
        if opt == '0' and not debug:
            mode = 'plain'
        elif opt == '0' and debug:
            mode = 'debug'
        elif opt == '2' and debug:
            mode = 'debugoptimized'
        elif opt == '3' and not debug:
            mode = 'release'
        elif opt == 's' and debug:
            mode = 'minsize'
        else:
            mode = 'custom'
        self.builtins['buildtype'].set_value(mode)

    def get_all_compiler_options(self):
        # TODO think about cross and command-line interface. (Only .build is mentioned here.)
        yield self.compiler_options.build

    def _get_all_nonbuiltin_options(self):
        yield self.backend_options
        yield self.user_options
        yield from self.get_all_compiler_options()
        yield self.base_options

    def get_all_options(self):
        return chain([self.builtins], self._get_all_nonbuiltin_options())

    def validate_option_value(self, option_name, override_value):
        for opts in self.get_all_options():
            if option_name in opts:
                opt = opts[option_name]
                try:
                    return opt.validate_value(override_value)
                except MesonException as e:
                    raise type(e)(('Validation failed for option %s: ' % option_name) + str(e)) \
                        .with_traceback(sys.exc_into()[2])
        raise MesonException('Tried to validate unknown option %s.' % option_name)

    def get_external_args(self, for_machine: MachineChoice, lang):
        return self.compiler_options[for_machine][lang + '_args'].value

    def get_external_link_args(self, for_machine: MachineChoice, lang):
        return self.compiler_options[for_machine][lang + '_link_args'].value

    def merge_user_options(self, options):
        for (name, value) in options.items():
            if name not in self.user_options:
                self.user_options[name] = value
            else:
                oldval = self.user_options[name]
                if type(oldval) != type(value):
                    self.user_options[name] = value

    def set_options(self, options, subproject='', warn_unknown=True):
        # Set prefix first because it's needed to sanitize other options
        prefix = self.builtins['prefix'].value
        if 'prefix' in options:
            prefix = self.sanitize_prefix(options['prefix'])
            self.builtins['prefix'].set_value(prefix)
            for key in builtin_dir_noprefix_options:
                if key not in options:
                    self.builtins[key].set_value(builtin_options[key].prefixed_default(key, prefix))

        unknown_options = []
        for k, v in options.items():
            if k == 'prefix':
                pass
            elif k in self.builtins:
                self.set_builtin_option(k, v)
            else:
                for opts in self._get_all_nonbuiltin_options():
                    if k in opts:
                        tgt = opts[k]
                        tgt.set_value(v)
                        break
                else:
                    unknown_options.append(k)
        if unknown_options and warn_unknown:
            unknown_options = ', '.join(sorted(unknown_options))
            sub = 'In subproject {}: '.format(subproject) if subproject else ''
            mlog.warning('{}Unknown options: "{}"'.format(sub, unknown_options))

    def set_default_options(self, default_options, subproject, env):
        # Set defaults first from conf files (cross or native), then
        # override them as nec as necessary.
        for k, v in env.paths.host:
            if v is not None:
                env.cmd_line_options.setdefault(k, v)

        # Set default options as if they were passed to the command line.
        # Subprojects can only define default for user options.
        from . import optinterpreter
        for k, v in default_options.items():
            if subproject:
                if optinterpreter.is_invalid_name(k, log=False):
                    continue
                k = subproject + ':' + k
            env.cmd_line_options.setdefault(k, v)

        # Create a subset of cmd_line_options, keeping only options for this
        # subproject. Also take builtin options if it's the main project.
        # Language and backend specific options will be set later when adding
        # languages and setting the backend (builtin options must be set first
        # to know which backend we'll use).
        options = {}

        # Some options default to environment variables if they are
        # unset, set those now. These will either be overwritten
        # below, or they won't. These should only be set on the first run.
        if env.first_invocation:
            p_env = os.environ.get('PKG_CONFIG_PATH')
            if p_env:
                options['pkg_config_path'] = p_env.split(':')

        for k, v in env.cmd_line_options.items():
            if subproject:
                if not k.startswith(subproject + ':'):
                    continue
            elif k not in builtin_options:
                if ':' in k:
                    continue
                if optinterpreter.is_invalid_name(k, log=False):
                    continue
            options[k] = v

        self.set_options(options, subproject)

    def process_new_compilers(self, lang: str, comp, cross_comp, env):
        from . import compilers

        self.compilers[lang] = comp
        if cross_comp is not None:
            self.cross_compilers[lang] = cross_comp

        # Native compiler always exist so always add its options.
        new_options_for_build = comp.get_and_default_options(env.properties.build)
        if cross_comp is not None:
            new_options_for_host = cross_comp.get_and_default_options(env.properties.host)
        else:
            new_options_for_host = new_options_for_build

        opts_machines_list = [
            (new_options_for_build, MachineChoice.BUILD),
            (new_options_for_host, MachineChoice.HOST),
        ]

        optprefix = lang + '_'
        for new_options, for_machine in opts_machines_list:
            for k, o in new_options.items():
                if not k.startswith(optprefix):
                    raise MesonException('Internal error, %s has incorrect prefix.' % k)
                if (env.machines.matches_build_machine(for_machine) and
                        k in env.cmd_line_options):
                    # TODO think about cross and command-line interface.
                    o.set_value(env.cmd_line_options[k])
                self.compiler_options[for_machine].setdefault(k, o)

        enabled_opts = []
        for optname in comp.base_options:
            if optname in self.base_options:
                continue
            oobj = compilers.base_options[optname]
            if optname in env.cmd_line_options:
                oobj.set_value(env.cmd_line_options[optname])
                enabled_opts.append(optname)
            self.base_options[optname] = oobj
        self.emit_base_options_warnings(enabled_opts)

    def emit_base_options_warnings(self, enabled_opts: list):
        if 'b_bitcode' in enabled_opts:
            mlog.warning('Base option \'b_bitcode\' is enabled, which is incompatible with many linker options. Incompatible options such as such as \'b_asneeded\' have been disabled.')
            mlog.warning('Please see https://mesonbuild.com/Builtin-options.html#Notes_about_Apple_Bitcode_support for more details.')

class CmdLineFileParser(configparser.ConfigParser):
    def __init__(self):
        # We don't want ':' as key delimiter, otherwise it would break when
        # storing subproject options like "subproject:option=value"
        super().__init__(delimiters=['='])

def get_cmd_line_file(build_dir):
    return os.path.join(build_dir, 'meson-private', 'cmd_line.txt')

def read_cmd_line_file(build_dir, options):
    filename = get_cmd_line_file(build_dir)
    config = CmdLineFileParser()
    config.read(filename)

    # Do a copy because config is not really a dict. options.cmd_line_options
    # overrides values from the file.
    d = dict(config['options'])
    d.update(options.cmd_line_options)
    options.cmd_line_options = d

    properties = config['properties']
    if not options.cross_file:
        options.cross_file = ast.literal_eval(properties.get('cross_file', '[]'))
    if not options.native_file:
        # This will be a string in the form: "['first', 'second', ...]", use
        # literal_eval to get it into the list of strings.
        options.native_file = ast.literal_eval(properties.get('native_file', '[]'))

def write_cmd_line_file(build_dir, options):
    filename = get_cmd_line_file(build_dir)
    config = CmdLineFileParser()

    properties = {}
    if options.cross_file:
        properties['cross_file'] = options.cross_file
    if options.native_file:
        properties['native_file'] = options.native_file

    config['options'] = options.cmd_line_options
    config['properties'] = properties
    with open(filename, 'w') as f:
        config.write(f)

def update_cmd_line_file(build_dir, options):
    filename = get_cmd_line_file(build_dir)
    config = CmdLineFileParser()
    config.read(filename)
    config['options'].update(options.cmd_line_options)
    with open(filename, 'w') as f:
        config.write(f)

def major_versions_differ(v1, v2):
    return v1.split('.')[0:2] != v2.split('.')[0:2]

def load(build_dir):
    filename = os.path.join(build_dir, 'meson-private', 'coredata.dat')
    load_fail_msg = 'Coredata file {!r} is corrupted. Try with a fresh build tree.'.format(filename)
    try:
        with open(filename, 'rb') as f:
            obj = pickle.load(f)
    except (pickle.UnpicklingError, EOFError):
        raise MesonException(load_fail_msg)
    except AttributeError:
        raise MesonException(
            "Coredata file {!r} references functions or classes that don't "
            "exist. This probably means that it was generated with an old "
            "version of meson.".format(filename))
    if not isinstance(obj, CoreData):
        raise MesonException(load_fail_msg)
    if major_versions_differ(obj.version, version):
        raise MesonException('Build directory has been generated with Meson version %s, '
                             'which is incompatible with current version %s.\n' %
                             (obj.version, version))
    return obj

def save(obj, build_dir):
    filename = os.path.join(build_dir, 'meson-private', 'coredata.dat')
    prev_filename = filename + '.prev'
    tempfilename = filename + '~'
    if major_versions_differ(obj.version, version):
        raise MesonException('Fatal version mismatch corruption.')
    if os.path.exists(filename):
        import shutil
        shutil.copyfile(filename, prev_filename)
    with open(tempfilename, 'wb') as f:
        pickle.dump(obj, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tempfilename, filename)
    return filename


def register_builtin_arguments(parser):
    for n, b in builtin_options.items():
        b.add_to_argparse(n, parser)
    parser.add_argument('-D', action='append', dest='projectoptions', default=[], metavar="option",
                        help='Set the value of an option, can be used several times to set multiple options.')

def create_options_dict(options):
    result = {}
    for o in options:
        try:
            (key, value) = o.split('=', 1)
        except ValueError:
            raise MesonException('Option {!r} must have a value separated by equals sign.'.format(o))
        result[key] = value
    return result

def parse_cmd_line_options(args):
    args.cmd_line_options = create_options_dict(args.projectoptions)

    # Merge builtin options set with --option into the dict.
    for name, builtin in builtin_options.items():
        names = [name]
        if builtin.separate_cross:
            names.append('cross_' + name)
        for name in names:
            value = getattr(args, name, None)
            if value is not None:
                if name in args.cmd_line_options:
                    cmdline_name = BuiltinOption.argparse_name_to_arg(name)
                    raise MesonException(
                        'Got argument {0} as both -D{0} and {1}. Pick one.'.format(name, cmdline_name))
                args.cmd_line_options[name] = value
                delattr(args, name)


_U = TypeVar('_U', bound=UserOption[_T])

class BuiltinOption(Generic[_T, _U]):

    """Class for a builtin option type.

    Currently doesn't support UserIntegerOption, or a few other cases.
    """

    def __init__(self, opt_type: Type[_U], description: str, default: Any, yielding: Optional[bool] = None, *,
                 choices: Any = None, separate_cross: bool = False):
        self.opt_type = opt_type
        self.description = description
        self.default = default
        self.choices = choices
        self.yielding = yielding
        self.separate_cross = separate_cross

    def init_option(self) -> _U:
        """Create an instance of opt_type and return it."""
        keywords = {'yielding': self.yielding, 'value': self.default}
        if self.choices:
            keywords['choices'] = self.choices
        return self.opt_type(self.description, **keywords)

    def _argparse_action(self) -> Optional[str]:
        if self.default is True:
            return 'store_false'
        elif self.default is False:
            return 'store_true'
        return None

    def _argparse_choices(self) -> Any:
        if self.opt_type is UserBooleanOption:
            return [True, False]
        elif self.opt_type is UserFeatureOption:
            return UserFeatureOption.static_choices
        return self.choices

    @staticmethod
    def argparse_name_to_arg(name: str) -> str:
        if name == 'warning_level':
            return '--warnlevel'
        else:
            return '--' + name.replace('_', '-')

    def prefixed_default(self, name: str, prefix: str = '') -> Any:
        if self.opt_type in [UserComboOption, UserIntegerOption]:
            return self.default
        try:
            return builtin_dir_noprefix_options[name][prefix]
        except KeyError:
            pass
        return self.default

    def add_to_argparse(self, name: str, parser: argparse.ArgumentParser) -> None:
        kwargs = {}

        c = self._argparse_choices()
        b = self._argparse_action()
        h = self.description
        if not b:
            h = '{} (default: {}).'.format(h.rstrip('.'), self.prefixed_default(name))
        else:
            kwargs['action'] = b
        if c and not b:
            kwargs['choices'] = c
        kwargs['default'] = argparse.SUPPRESS
        kwargs['dest'] = name

        cmdline_name = self.argparse_name_to_arg(name)
        parser.add_argument(cmdline_name, help=h, **kwargs)
        if self.separate_cross:
            kwargs['dest'] = 'cross_' + name
            parser.add_argument(self.argparse_name_to_arg('cross_' + name), help=h + ' (for host in cross compiles)', **kwargs)

# Update `docs/markdown/Builtin-options.md` after changing the options below
builtin_options = OrderedDict([
    # Directories
    ('prefix',     BuiltinOption(UserStringOption, 'Installation prefix', default_prefix())),
    ('bindir',     BuiltinOption(UserStringOption, 'Executable directory', 'bin')),
    ('datadir',    BuiltinOption(UserStringOption, 'Data file directory', 'share')),
    ('includedir', BuiltinOption(UserStringOption, 'Header file directory', 'include')),
    ('infodir',    BuiltinOption(UserStringOption, 'Info page directory', 'share/info')),
    ('libdir',     BuiltinOption(UserStringOption, 'Library directory', default_libdir())),
    ('libexecdir', BuiltinOption(UserStringOption, 'Library executable directory', default_libexecdir())),
    ('localedir',  BuiltinOption(UserStringOption, 'Locale data directory', 'share/locale')),
    ('localstatedir',   BuiltinOption(UserStringOption, 'Localstate data directory', 'var')),
    ('mandir',          BuiltinOption(UserStringOption, 'Manual page directory', 'share/man')),
    ('sbindir',         BuiltinOption(UserStringOption, 'System executable directory', 'sbin')),
    ('sharedstatedir',  BuiltinOption(UserStringOption, 'Architecture-independent data directory', 'com')),
    ('sysconfdir',      BuiltinOption(UserStringOption, 'Sysconf data directory', 'etc')),
    # Core options
    ('auto_features',   BuiltinOption(UserFeatureOption, "Override value of all 'auto' features", 'auto')),
    ('backend',         BuiltinOption(UserComboOption, 'Backend to use', 'ninja', choices=backendlist)),
    ('buildtype',       BuiltinOption(UserComboOption, 'Build type to use', 'debug',
                                      choices=['plain', 'debug', 'debugoptimized', 'release', 'minsize', 'custom'])),
    ('debug',           BuiltinOption(UserBooleanOption, 'Debug', True)),
    ('default_library', BuiltinOption(UserComboOption, 'Default library type', 'shared', choices=['shared', 'static', 'both'])),
    ('errorlogs',       BuiltinOption(UserBooleanOption, "Whether to print the logs from failing tests", True)),
    ('install_umask',   BuiltinOption(UserUmaskOption, 'Default umask to apply on permissions of installed files', '022')),
    ('layout',          BuiltinOption(UserComboOption, 'Build directory layout', 'mirror', choices=['mirror', 'flat'])),
    ('pkg_config_path', BuiltinOption(UserArrayOption, 'List of additional paths for pkg-config to search', [], separate_cross=True)),
    ('optimization',    BuiltinOption(UserComboOption, 'Optimization level', '0', choices=['0', 'g', '1', '2', '3', 's'])),
    ('stdsplit',        BuiltinOption(UserBooleanOption, 'Split stdout and stderr in test logs', True)),
    ('strip',           BuiltinOption(UserBooleanOption, 'Strip targets on install', False)),
    ('unity',           BuiltinOption(UserComboOption, 'Unity build', 'off', choices=['on', 'off', 'subprojects'])),
    ('warning_level',   BuiltinOption(UserComboOption, 'Compiler warning level to use', '1', choices=['0', '1', '2', '3'])),
    ('werror',          BuiltinOption(UserBooleanOption, 'Treat warnings as errors', False)),
    ('wrap_mode',       BuiltinOption(UserComboOption, 'Wrap mode', 'default', choices=['default', 'nofallback', 'nodownload', 'forcefallback'])),
])

# Special prefix-dependent defaults for installation directories that reside in
# a path outside of the prefix in FHS and common usage.
builtin_dir_noprefix_options = {
    'sysconfdir':     {'/usr': '/etc'},
    'localstatedir':  {'/usr': '/var',     '/usr/local': '/var/local'},
    'sharedstatedir': {'/usr': '/var/lib', '/usr/local': '/var/local/lib'},
}

forbidden_target_names = {'clean': None,
                          'clean-ctlist': None,
                          'clean-gcno': None,
                          'clean-gcda': None,
                          'coverage': None,
                          'coverage-text': None,
                          'coverage-xml': None,
                          'coverage-html': None,
                          'phony': None,
                          'PHONY': None,
                          'all': None,
                          'test': None,
                          'benchmark': None,
                          'install': None,
                          'uninstall': None,
                          'build.ninja': None,
                          'scan-build': None,
                          'reconfigure': None,
                          'dist': None,
                          'distcheck': None,
                          }
