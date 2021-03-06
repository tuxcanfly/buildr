import cgi
import errno
import mimetypes
import os
import posixpath
import re
import shutil
import stat
import sys
import tempfile
try:
    from urllib.request import urlretrieve
except ImportError:     # Python 2
    from urllib import urlretrieve

from optparse import make_option
from optparse import OptionParser, NO_DEFAULT
from os import path

import django
from mako.template import Template
from django.utils import archive
from django.utils._os import rmtree_errorhandler
from django.utils.crypto import get_random_string
from django.utils.importlib import import_module
from django.core.management.base import BaseCommand, CommandError
from django.core.management.commands.makemessages import handle_extensions


_drive_re = re.compile('^([a-z]):', re.I)
_url_drive_re = re.compile('^([a-z])[:|]', re.I)


class TemplateCommand(BaseCommand):
    """
    Copies either a Django application layout template or a Django project
    layout template into the specified directory.

    :param style: A color style object (see django.core.management.color).
    :param app_or_project: The string 'app' or 'project'.
    :param name: The name of the application or project.
    :param directory: The directory to which the template should be copied.
    :param options: The additional variables passed to project or app templates
    """
    args = "[name] [optional destination directory]"
    option_list = BaseCommand.option_list + (
        make_option('--template',
                    action='store', dest='template',
                    help='The dotted import path to load the template from.'),
        make_option('--extension', '-e', dest='extensions',
                    action='append', default=['py'],
                    help='The file extension(s) to render (default: "py"). '
                         'Separate multiple extensions with commas, or use '
                         '-e multiple times.'),
        make_option('--name', '-n', dest='files',
                    action='append', default=[],
                    help='The file name(s) to render. '
                         'Separate multiple extensions with commas, or use '
                         '-n multiple times.')
        )
    requires_model_validation = False
    # Can't import settings during this command, because they haven't
    # necessarily been created.
    can_import_settings = False
    # The supported URL schemes
    url_schemes = ['http', 'https', 'ftp']

    def handle(self, app_or_project, target=None, **options):
        self.app_or_project = app_or_project
        self.paths_to_remove = []
        self.verbosity = int(options.get('verbosity'))

        top_dir = path.join(os.getcwd(), target)
        try:
            os.makedirs(top_dir)
        except OSError as e:
            if e.errno == errno.EEXIST:
                message = "'%s' already exists" % top_dir
            else:
                message = e
            raise CommandError(message)

        extensions = tuple(
            handle_extensions(options.get('extensions'), ignored=()))
        extra_files = []
        for file in options.get('files'):
            extra_files.extend(map(lambda x: x.strip(), file.split(',')))
        if self.verbosity >= 2:
            self.stdout.write("Rendering %s template files with "
                              "extensions: %s\n" %
                              (app_or_project, ', '.join(extensions)))
            self.stdout.write("Rendering %s template files with "
                              "filenames: %s\n" %
                              (app_or_project, ', '.join(extra_files)))

        base_subdir = '%s_template' % app_or_project
        base_directory = '%s_directory' % app_or_project

        context = dict(options, **{
            base_directory: top_dir,
        })

        # Setup a stub settings environment for template rendering
        from django.conf import settings
        if not settings.configured:
            settings.configure()

        project_template_dir = self.handle_template(options.get('template'),
                                            base_subdir)
        plugins = options.get('plugins')
        plugin_dirs = [plugin.files for plugin in plugins if plugin.files]
        template_dirs = [project_template_dir] + plugin_dirs
        for template_dir in template_dirs:
            for root, dirs, files in os.walk(template_dir):

                prefix_length = len(template_dir) + 1
                path_rest = root[prefix_length:]
                relative_dir = path_rest
                if relative_dir:
                    target_dir = path.join(top_dir, relative_dir)
                    if not path.exists(target_dir):
                        os.mkdir(target_dir)

                for dirname in dirs[:]:
                    if dirname.startswith('.') or dirname == '__pycache__':
                        dirs.remove(dirname)

                for filename in files:
                    if filename.endswith(('.pyo', '.pyc', '.py.class')):
                        # Ignore some files as they cause various breakages.
                        continue
                    old_path = path.join(root, filename)
                    new_path = path.join(top_dir, relative_dir, filename)
                    if path.exists(new_path):
                        raise CommandError("%s already exists, overlaying a "
                                        "project or app into an existing "
                                        "directory won't replace conflicting "
                                        "files" % new_path)

                    # Only render the Python files, as we don't want to
                    # accidentally render Django templates files
                    with open(old_path, 'rb') as template_file:
                        content = template_file.read()
                    if filename.endswith(extensions) or filename in extra_files:
                        content = content.decode('utf-8')
                        template = Template(content)
                        content = template.render(**context)
                        content = content.encode('utf-8')
                    with open(new_path, 'wb') as new_file:
                        new_file.write(content)

                    if self.verbosity >= 2:
                        self.stdout.write("Creating %s\n" % new_path)
                    try:
                        shutil.copymode(old_path, new_path)
                        self.make_writeable(new_path)
                    except OSError:
                        self.stderr.write(
                            "Notice: Couldn't set permission bits on %s. You're "
                            "probably using an uncommon filesystem setup. No "
                            "problem." % new_path, self.style.NOTICE)

        if self.paths_to_remove:
            if self.verbosity >= 2:
                self.stdout.write("Cleaning up temporary files.\n")
            for path_to_remove in self.paths_to_remove:
                if path.isfile(path_to_remove):
                    os.remove(path_to_remove)
                else:
                    shutil.rmtree(path_to_remove,
                                  onerror=rmtree_errorhandler)

    def handle_template(self, template, subdir):
        """
        Determines where the app or project templates are.
        Use django.__path__[0] as the default because we don't
        know into which directory Django has been installed.
        """
        if template is None:
            return path.join(django.__path__[0], 'conf', subdir)
        else:
            if template.startswith('file://'):
                template = template[7:]
            expanded_template = path.expanduser(template)
            expanded_template = path.normpath(expanded_template)
            if path.isdir(expanded_template):
                return expanded_template
            if self.is_url(template):
                # downloads the file and returns the path
                absolute_path = self.download(template)
            else:
                absolute_path = path.abspath(expanded_template)
            if path.exists(absolute_path):
                return self.extract(absolute_path)

        raise CommandError("couldn't handle %s template %s." %
                           (self.app_or_project, template))

    def download(self, url):
        """
        Downloads the given URL and returns the file name.
        """
        def cleanup_url(url):
            tmp = url.rstrip('/')
            filename = tmp.split('/')[-1]
            if url.endswith('/'):
                display_url  = tmp + '/'
            else:
                display_url = url
            return filename, display_url

        prefix = 'django_%s_template_' % self.app_or_project
        tempdir = tempfile.mkdtemp(prefix=prefix, suffix='_download')
        self.paths_to_remove.append(tempdir)
        filename, display_url = cleanup_url(url)

        if self.verbosity >= 2:
            self.stdout.write("Downloading %s\n" % display_url)
        try:
            the_path, info = urlretrieve(url, path.join(tempdir, filename))
        except IOError as e:
            raise CommandError("couldn't download URL %s to %s: %s" %
                               (url, filename, e))

        used_name = the_path.split('/')[-1]

        # Trying to get better name from response headers
        content_disposition = info.get('content-disposition')
        if content_disposition:
            _, params = cgi.parse_header(content_disposition)
            guessed_filename = params.get('filename') or used_name
        else:
            guessed_filename = used_name

        # Falling back to content type guessing
        ext = self.splitext(guessed_filename)[1]
        content_type = info.get('content-type')
        if not ext and content_type:
            ext = mimetypes.guess_extension(content_type)
            if ext:
                guessed_filename += ext

        # Move the temporary file to a filename that has better
        # chances of being recognnized by the archive utils
        if used_name != guessed_filename:
            guessed_path = path.join(tempdir, guessed_filename)
            shutil.move(the_path, guessed_path)
            return guessed_path

        # Giving up
        return the_path

    def splitext(self, the_path):
        """
        Like os.path.splitext, but takes off .tar, too
        """
        base, ext = posixpath.splitext(the_path)
        if base.lower().endswith('.tar'):
            ext = base[-4:] + ext
            base = base[:-4]
        return base, ext

    def extract(self, filename):
        """
        Extracts the given file to a temporarily and returns
        the path of the directory with the extracted content.
        """
        prefix = 'django_%s_template_' % self.app_or_project
        tempdir = tempfile.mkdtemp(prefix=prefix, suffix='_extract')
        self.paths_to_remove.append(tempdir)
        if self.verbosity >= 2:
            self.stdout.write("Extracting %s\n" % filename)
        try:
            archive.extract(filename, tempdir)
            return tempdir
        except (archive.ArchiveException, IOError) as e:
            raise CommandError("couldn't extract file %s to %s: %s" %
                               (filename, tempdir, e))

    def is_url(self, template):
        """
        Returns True if the name looks like a URL
        """
        if ':' not in template:
            return False
        scheme = template.split(':', 1)[0].lower()
        return scheme in self.url_schemes

    def make_writeable(self, filename):
        """
        Make sure that the file is writeable.
        Useful if our source is read-only.
        """
        if sys.platform.startswith('java'):
            # On Jython there is no os.access()
            return
        if not os.access(filename, os.W_OK):
            st = os.stat(filename)
            new_permissions = stat.S_IMODE(st.st_mode) | stat.S_IWUSR
            os.chmod(filename, new_permissions)


class Command(TemplateCommand):
    help = ("Creates a Django project directory structure for the given "
            "project name in the current directory or optionally in the "
            "given directory.")

    def handle(self, target=None, *args, **options):
        defaults = {}
        for opt in self.option_list:
            if opt.default is NO_DEFAULT:
                defaults[opt.dest] = None
            else:
                defaults[opt.dest] = opt.default
        defaults.update(options)
        super(Command, self).handle('project', target, **defaults)
