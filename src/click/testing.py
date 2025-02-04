import contextlib
import io
import os
import shlex
import shutil
import sys
import tempfile

from . import formatting
from . import termui
from . import utils
from ._compat import _find_binary_reader


class EchoingStdin:
    def __init__(self, input, output):
        self._input = input
        self._output = output

    def __getattr__(self, x):
        return getattr(self._input, x)

    def _echo(self, rv):
        self._output.write(rv)
        return rv

    def read(self, n=-1):
        return self._echo(self._input.read(n))

    def readline(self, n=-1):
        return self._echo(self._input.readline(n))

    def readlines(self):
        return [self._echo(x) for x in self._input.readlines()]

    def __iter__(self):
        return iter(self._echo(x) for x in self._input)

    def __repr__(self):
        return repr(self._input)


class _NamedTextIOWrapper(io.TextIOWrapper):
    def __init__(self, buffer, name=None, mode=None, **kwargs):
        super().__init__(buffer, **kwargs)
        self._name = name
        self._mode = mode

    @property
    def name(self):
        return self._name

    @property
    def mode(self):
        return self._mode


def make_input_stream(input, charset):
    # Is already an input stream.
    if hasattr(input, "read"):
        rv = _find_binary_reader(input)

        if rv is not None:
            return rv

        raise TypeError("Could not find binary reader for input stream.")

    if input is None:
        input = b""
    elif not isinstance(input, bytes):
        input = input.encode(charset)

    return io.BytesIO(input)


class Result:
    """Holds the captured result of an invoked CLI script."""

    def __init__(
        self,
        runner,
        stdout_bytes,
        stderr_bytes,
        return_value,
        exit_code,
        exception,
        exc_info=None,
    ):
        #: The runner that created the result
        self.runner = runner
        #: The standard output as bytes.
        self.stdout_bytes = stdout_bytes
        #: The standard error as bytes, or None if not available
        self.stderr_bytes = stderr_bytes
        #: The value returned from the invoked command.
        #:
        #: .. versionadded:: 8.0
        self.return_value = return_value
        #: The exit code as integer.
        self.exit_code = exit_code
        #: The exception that happened if one did.
        self.exception = exception
        #: The traceback
        self.exc_info = exc_info

    @property
    def output(self):
        """The (standard) output as unicode string."""
        return self.stdout

    @property
    def stdout(self):
        """The standard output as unicode string."""
        return self.stdout_bytes.decode(self.runner.charset, "replace").replace(
            "\r\n", "\n"
        )

    @property
    def stderr(self):
        """The standard error as unicode string."""
        if self.stderr_bytes is None:
            raise ValueError("stderr not separately captured")
        return self.stderr_bytes.decode(self.runner.charset, "replace").replace(
            "\r\n", "\n"
        )

    def __repr__(self):
        exc_str = repr(self.exception) if self.exception else "okay"
        return f"<{type(self).__name__} {exc_str}>"


class CliRunner:
    """The CLI runner provides functionality to invoke a Click command line
    script for unittesting purposes in a isolated environment.  This only
    works in single-threaded systems without any concurrency as it changes the
    global interpreter state.

    :param charset: the character set for the input and output data.
    :param env: a dictionary with environment variables for overriding.
    :param echo_stdin: if this is set to `True`, then reading from stdin writes
                       to stdout.  This is useful for showing examples in
                       some circumstances.  Note that regular prompts
                       will automatically echo the input.
    :param mix_stderr: if this is set to `False`, then stdout and stderr are
                       preserved as independent streams.  This is useful for
                       Unix-philosophy apps that have predictable stdout and
                       noisy stderr, such that each may be measured
                       independently
    """

    def __init__(self, charset="utf-8", env=None, echo_stdin=False, mix_stderr=True):
        self.charset = charset
        self.env = env or {}
        self.echo_stdin = echo_stdin
        self.mix_stderr = mix_stderr

    def get_default_prog_name(self, cli):
        """Given a command object it will return the default program name
        for it.  The default is the `name` attribute or ``"root"`` if not
        set.
        """
        return cli.name or "root"

    def make_env(self, overrides=None):
        """Returns the environment overrides for invoking a script."""
        rv = dict(self.env)
        if overrides:
            rv.update(overrides)
        return rv

    @contextlib.contextmanager
    def isolation(self, input=None, env=None, color=False):
        """A context manager that sets up the isolation for invoking of a
        command line tool.  This sets up stdin with the given input data
        and `os.environ` with the overrides from the given dictionary.
        This also rebinds some internals in Click to be mocked (like the
        prompt functionality).

        This is automatically done in the :meth:`invoke` method.

        :param input: the input stream to put into sys.stdin.
        :param env: the environment overrides as dictionary.
        :param color: whether the output should contain color codes. The
                      application can still override this explicitly.

        .. versionchanged:: 8.0
            ``stderr`` is opened with ``errors="backslashreplace"``
            instead of the default ``"strict"``.

        .. versionchanged:: 4.0
            Added the ``color`` parameter.
        """
        input = make_input_stream(input, self.charset)

        old_stdin = sys.stdin
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        old_forced_width = formatting.FORCED_WIDTH
        formatting.FORCED_WIDTH = 80

        env = self.make_env(env)

        bytes_output = io.BytesIO()

        if self.echo_stdin:
            input = EchoingStdin(input, bytes_output)

        sys.stdin = input = _NamedTextIOWrapper(
            input, encoding=self.charset, name="<stdin>", mode="r"
        )
        sys.stdout = _NamedTextIOWrapper(
            bytes_output, encoding=self.charset, name="<stdout>", mode="w"
        )

        bytes_error = None
        if self.mix_stderr:
            sys.stderr = sys.stdout
        else:
            bytes_error = io.BytesIO()
            sys.stderr = _NamedTextIOWrapper(
                bytes_error,
                encoding=self.charset,
                name="<stderr>",
                mode="w",
                errors="backslashreplace",
            )

        def visible_input(prompt=None):
            sys.stdout.write(prompt or "")
            val = input.readline().rstrip("\r\n")
            sys.stdout.write(f"{val}\n")
            sys.stdout.flush()
            return val

        def hidden_input(prompt=None):
            sys.stdout.write(f"{prompt or ''}\n")
            sys.stdout.flush()
            return input.readline().rstrip("\r\n")

        def _getchar(echo):
            char = sys.stdin.read(1)
            if echo:
                sys.stdout.write(char)
                sys.stdout.flush()
            return char

        default_color = color

        def should_strip_ansi(stream=None, color=None):
            if color is None:
                return not default_color
            return not color

        old_visible_prompt_func = termui.visible_prompt_func
        old_hidden_prompt_func = termui.hidden_prompt_func
        old__getchar_func = termui._getchar
        old_should_strip_ansi = utils.should_strip_ansi
        termui.visible_prompt_func = visible_input
        termui.hidden_prompt_func = hidden_input
        termui._getchar = _getchar
        utils.should_strip_ansi = should_strip_ansi

        old_env = {}
        try:
            for key, value in env.items():
                old_env[key] = os.environ.get(key)
                if value is None:
                    try:
                        del os.environ[key]
                    except Exception:
                        pass
                else:
                    os.environ[key] = value
            yield (bytes_output, bytes_error)
        finally:
            for key, value in old_env.items():
                if value is None:
                    try:
                        del os.environ[key]
                    except Exception:
                        pass
                else:
                    os.environ[key] = value
            sys.stdout = old_stdout
            sys.stderr = old_stderr
            sys.stdin = old_stdin
            termui.visible_prompt_func = old_visible_prompt_func
            termui.hidden_prompt_func = old_hidden_prompt_func
            termui._getchar = old__getchar_func
            utils.should_strip_ansi = old_should_strip_ansi
            formatting.FORCED_WIDTH = old_forced_width

    def invoke(
        self,
        cli,
        args=None,
        input=None,
        env=None,
        catch_exceptions=True,
        color=False,
        **extra,
    ):
        """Invokes a command in an isolated environment.  The arguments are
        forwarded directly to the command line script, the `extra` keyword
        arguments are passed to the :meth:`~clickpkg.Command.main` function of
        the command.

        This returns a :class:`Result` object.

        :param cli: the command to invoke
        :param args: the arguments to invoke. It may be given as an iterable
                     or a string. When given as string it will be interpreted
                     as a Unix shell command. More details at
                     :func:`shlex.split`.
        :param input: the input data for `sys.stdin`.
        :param env: the environment overrides.
        :param catch_exceptions: Whether to catch any other exceptions than
                                 ``SystemExit``.
        :param extra: the keyword arguments to pass to :meth:`main`.
        :param color: whether the output should contain color codes. The
                      application can still override this explicitly.

        .. versionchanged:: 8.0
            The result object has the ``return_value`` attribute with
            the value returned from the invoked command.

        .. versionchanged:: 4.0
            Added the ``color`` parameter.

        .. versionchanged:: 3.0
            Added the ``catch_exceptions`` parameter.

        .. versionchanged:: 3.0
            The result object has the ``exc_info`` attribute with the
            traceback if available.
        """
        exc_info = None
        with self.isolation(input=input, env=env, color=color) as outstreams:
            return_value = None
            exception = None
            exit_code = 0

            if isinstance(args, str):
                args = shlex.split(args)

            try:
                prog_name = extra.pop("prog_name")
            except KeyError:
                prog_name = self.get_default_prog_name(cli)

            try:
                return_value = cli.main(args=args or (), prog_name=prog_name, **extra)
            except SystemExit as e:
                exc_info = sys.exc_info()
                exit_code = e.code
                if exit_code is None:
                    exit_code = 0

                if exit_code != 0:
                    exception = e

                if not isinstance(exit_code, int):
                    sys.stdout.write(str(exit_code))
                    sys.stdout.write("\n")
                    exit_code = 1

            except Exception as e:
                if not catch_exceptions:
                    raise
                exception = e
                exit_code = 1
                exc_info = sys.exc_info()
            finally:
                sys.stdout.flush()
                stdout = outstreams[0].getvalue()
                if self.mix_stderr:
                    stderr = None
                else:
                    stderr = outstreams[1].getvalue()

        return Result(
            runner=self,
            stdout_bytes=stdout,
            stderr_bytes=stderr,
            return_value=return_value,
            exit_code=exit_code,
            exception=exception,
            exc_info=exc_info,
        )

    @contextlib.contextmanager
    def isolated_filesystem(self, temp_dir=None):
        """A context manager that creates a temporary directory and
        changes the current working directory to it. This isolates tests
        that affect the contents of the CWD to prevent them from
        interfering with each other.

        :param temp_dir: Create the temporary directory under this
            directory. If given, the created directory is not removed
            when exiting.

        .. versionchanged:: 8.0
            Added the ``temp_dir`` parameter.
        """
        cwd = os.getcwd()
        t = tempfile.mkdtemp(dir=temp_dir)
        os.chdir(t)

        try:
            yield t
        finally:
            os.chdir(cwd)

            if temp_dir is None:
                try:
                    shutil.rmtree(t)
                except OSError:  # noqa: B014
                    pass
