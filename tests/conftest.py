"""Windows: tests open SQLite files inside temp folders and never close them. Windows refuses to
delete an open file (Linux doesn't care), so the temp-folder cleanup failed the test. Skip
locked files during cleanup instead; the OS clears %TEMP% eventually."""
import functools
import tempfile

tempfile.TemporaryDirectory = functools.partial(tempfile.TemporaryDirectory, ignore_cleanup_errors=True)
